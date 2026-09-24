"""文档树抓取：优先走 API，浏览器旁听作兜底。

主路径（纯 HTTP，见 probe_via_api）：
  1. get_node(root_token)  → 根文档的 obj_token
  2. 从 root_token 起 BFS：get_tree 每次只给**一层**，逐层展开才是整棵树

⚠️ 这里踩过一次大坑：`get_tree` 并不递归，接口只返回「本节点 + 直接子节点」。
   早期版本调一次就把结果当成全树，于是 41 篇的库只导出了 15 篇，26 篇子文档
   （接口文档底下的 9 篇、各章节的作业/总结）一篇都没下来。现在改成 BFS。

兜底路径（LinkCollector）：浏览器打开根文档时，页面自己会去请求文档树接口，
我们在一旁把响应抄下来。这是**被动监听网络**，不是点按钮，所以稳定得多。
只有 API 路径拿不到东西时才走这里。

注意：Playwright 的事件包装器不会 await 协程，异步回调会静默失效，
所以监听器写成同步函数，内部用 create_task 把异步读取 body 派发出去。
"""

import asyncio
import re
from urllib.parse import urljoin

from .api import ApiError, dig

NODE_RE = re.compile(r"(/wiki/[A-Za-z0-9]+|/docx/[A-Za-z0-9]+)")

# 可能与文档树相关的请求，命中就记进诊断列表
API_KEYS = ("wiki", "node", "space/api", "drive", "token", "object")

TREE_API = "wiki/v2/tree/"

# BFS 上限。接口异常时（比如互相认爹）不至于无限展开，正常库远达不到。
MAX_NODES = 2000


def _tree_nodes_of(body):
    """从响应里挖出 tree.nodes 那一块；挖不到返回 {}。

    响应实测嵌两层 data.data，这里做成能兼容两种深度。
    """
    cur, nodes = body, None
    while isinstance(cur, dict) and nodes is None:
        nodes = dig(cur, "tree", "nodes")
        cur = cur.get("data")
    return nodes if isinstance(nodes, dict) else {}


async def probe_via_api(api, root_token, logger, max_nodes=MAX_NODES):
    """走 API 拿整棵树（BFS 递归展开全部层级）。

    返回 (root_obj_token, nodes, root_title)，
    nodes 是 [(node_token, obj_token, title, parent_token)]，**不含根文档自身**。
    根文档的标题单独回传：BFS 是从根往下走的，根本身不会进 nodes。

    为什么要 BFS 而不是一次调用：`get_tree` 只返回一层。子节点下面还有子节点时，
    必须拿子节点的 token 再问一次。判断还有没有下一层可以看 has_child，
    但这里不去信它 —— 直接问一遍，空列表就是到底了（少一次信任，少一个坑）。
    """
    node = await api.get_node(root_token)
    root_obj = str(node.get("obj_token") or "")
    root_title = str(node.get("title") or "")
    logger.log(f"根文档 obj_token = {root_obj}")

    out, seen = [], {root_token}
    queue = [root_token]
    last_milestone = 0
    while queue:
        tok = queue.pop(0)
        try:
            _, children = await api.get_tree(tok)
        except ApiError as e:
            if tok == root_token:
                # 连根节点的子节点都列不出来，就不是「某个子树有问题」，
                # 而是凭证/权限整体不合格。必须**往上抛**：这里静默跳过的话，
                # nodes 会变成空表，上层以为「根文档底下没有子文档」，于是
                # 只导出一篇就收工 —— 又是一种不吭声的少抓。交给上层重登一次
                # 再试，仍不行就明确报错。
                raise
            logger.log(f"  ! 取 {tok} 的子节点失败，跳过它的子树：{e}")
            continue
        for c in children:
            ctok = str(c.get("wiki_token") or "")
            if not ctok or ctok in seen:
                continue  # 去重，顺带防住「互相认爹」的死循环
            seen.add(ctok)
            out.append((
                ctok,
                str(c.get("obj_token") or ""),
                str(c.get("title") or ""),
                tok,
            ))
            queue.append(ctok)
            if len(out) >= max_nodes:
                logger.log(f"  ! 已达节点上限 {max_nodes}，剩下的没有抓。")
                queue.clear()
                break
        if len(out) - last_milestone >= 25:
            last_milestone = len(out)
            logger.log(f"  …已抓到 {len(out)} 篇，还在往下展开")

    logger.log(f"文档树共 {len(out)} 篇（全部层级已展开）。")
    return root_obj, out, root_title


def build_paths(root_token, nodes):
    """把扁平节点表还原成「该放哪个目录」。

    返回 {node_token: [目录名, ...]}，空列表表示放顶层。

    规则一句话：**有子文档的文档会折进一个以自己命名的文件夹，自己也待在里面**；
    没有子文档的就是一个平铺的 PDF。

        首页                          → downloads/首页.pdf
        ├─ 第1章-Java基础-…           → downloads/第1章-Java基础-…/第1章-Java基础-….pdf
        │  ├─ 作业                    → downloads/第1章-Java基础-…/作业.pdf
        │  └─ day01总结               → downloads/第1章-Java基础-…/day01总结.pdf
        └─ 第11章-轻客管家-…          → downloads/第11章-轻客管家-….pdf（没子文档，平铺）

    根文档是**特例**：它自己做顶层的一个 PDF，不折进同名文件夹 ——
    否则 downloads/ 下会套一个「首页/」，多一层还没什么信息量。

    祖先链里的每一环都必然有子文档（它的孩子就是链上的下一环），所以直接取
    「去掉根文档的祖先标题」就是目录链，不用逐层判断，只需要额外判断自己这一层。

    找不到父节点（浏览器兜底抓来的、links.txt 手工补的）就当顶层处理。
    """
    parent = {tok: ptok for tok, _, _, ptok in nodes}
    title = {tok: ti for tok, _, ti, _ in nodes}
    # 谁有子文档。根文档也会出现在这里，但下面只查非根节点，不影响。
    has_child = {ptok for ptok in parent.values() if ptok}

    out = {}
    for tok in parent:
        chain, cur, guard = [], parent.get(tok), 0
        while cur and cur != root_token and guard < 64:
            if title.get(cur):
                chain.append(title[cur])
            cur = parent.get(cur)
            guard += 1  # 防住环：parent 互相指也不会转不出来
        chain.reverse()
        if tok in has_child and title.get(tok):
            chain.append(title[tok])  # 自己有子文档 → 连自己也进这个文件夹
        out[tok] = chain
    return out


class LinkCollector:
    """浏览器旁听兜底：从页面的网络响应里抄下文档树。"""

    def __init__(self, logger):
        self.log = logger
        self.tree_nodes = []   # [(node_token, obj_token, title, parent_token)]，保序
        self.node_titles = {}  # node_token -> title
        self.api_hits = []
        self._tasks = set()

    def attach(self, page):
        page.on("response", self._on_response)

    # ---------------- 网络监听 ----------------

    def _on_response(self, resp):
        """同步回调：只做判定与派发，真正读 body 交给后台任务。"""
        url = resp.url
        low = url.lower()
        if any(k in low for k in API_KEYS):
            self.api_hits.append(url[:160])
        if TREE_API not in low:
            return
        try:
            task = asyncio.create_task(self._read_tree(resp))
        except RuntimeError:
            return  # 没有运行中的事件循环，忽略
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _read_tree(self, resp):
        try:
            body = await resp.json()
        except Exception:
            return
        # 优先只认 tree.nodes 这一块。以前是**无差别递归整个响应体**，只要某个
        # 字典带 wiki_token + title 就当成节点 —— 结果把 data.space.home_page_v2
        # （知识库首页，恰好也是这个形状）也收了进来，凭空多出一篇「AI工程化基础」。
        # 挖不到 tree.nodes 时才退回老的全量递归。
        nodes = _tree_nodes_of(body)
        if nodes:
            for n in nodes.values():
                if isinstance(n, dict):
                    self._add_node(n)
            return
        self._extract_nodes(body)

    def _add_node(self, obj):
        tok = obj.get("wiki_token") or obj.get("node_token")
        title = obj.get("title")
        if not tok or not title:
            return
        tok = str(tok).strip()
        if tok in self.node_titles:
            return
        self.tree_nodes.append((
            tok,
            str(obj.get("obj_token") or ""),
            str(title),
            str(obj.get("parent_wiki_token") or ""),
        ))
        self.node_titles[tok] = title

    def _extract_nodes(self, obj):
        """递归提取节点（兜底用，会把非树字段也扫进来，谨慎使用）。"""
        if isinstance(obj, dict):
            self._add_node(obj)
            for v in obj.values():
                self._extract_nodes(v)
        elif isinstance(obj, list):
            for v in obj:
                self._extract_nodes(v)

    async def wait_for_tree(self, timeout=10.0, interval=0.5, stable_rounds=3):
        """等文档树节点数连续 stable_rounds 次不再增长。

        不能只等「第一个节点出现」：文档树是分页拉的，第一批响应回来后还会陆续
        补几批。等数量稳定才能把子节点收全。
        """
        last = -1
        same = 0
        waited = 0.0
        while waited < timeout:
            n = len(self.tree_nodes)
            if n > 0 and n == last:
                same += 1
                if same >= stable_rounds:
                    return True
            else:
                same = 0
            last = n
            await asyncio.sleep(interval)
            waited += interval
        return len(self.tree_nodes) > 0

    # ---------------- 链接汇总 ----------------

    async def collect(self, page, root_url, host):
        """汇总子文档节点（不含根文档）。

        返回 [(node_token, obj_token, title, parent_token)]。
        旁听只覆盖页面请求过的范围，层级可能不全，所以只是兜底。
        """
        self.log.log("开始抓取子文档链接…")
        out, seen = [], set()
        root_tok = root_url.rstrip("/").rsplit("/", 1)[-1]

        # 1) 网络文档树（最可靠）
        if self.tree_nodes:
            self.log.log(f"从网络文档树捕获到 {len(self.tree_nodes)} 个节点。")
            for tok, obj_tok, title, ptok in self.tree_nodes:
                tok = (tok or "").strip()
                if not tok or tok == root_tok or tok in seen:
                    continue
                seen.add(tok)
                out.append((tok, obj_tok, title, ptok))
                self.log.log(f"  ✓ 子文档：{title} -> {tok}")

        # 2) 兜底：全页 a 链接 + token 属性（拿不到 obj_token，留给上层再换）
        if not out:
            self.log.log("文档树未命中，回退到 DOM 抓取…")
            for href in await self.eval_list(
                page,
                "() => Array.from(document.querySelectorAll('a[href]'))"
                ".map(a => a.href || '')",
            ):
                m = NODE_RE.search(href)
                if not m:
                    continue
                node = m.group(1)
                if node in seen:
                    continue
                seen.add(node)
                out.append((node.rsplit("/", 1)[-1], "", "", ""))
            for attr in ("data-node-token", "data-node-id", "data-token"):
                for t in await self.eval_list(
                    page,
                    f"() => Array.from(document.querySelectorAll('[{attr}]'))"
                    f".map(el => el.getAttribute('{attr}') || '')",
                ):
                    t = (t or "").strip()
                    if t and t not in seen:
                        seen.add(t)
                        out.append((t, "", "", ""))

        self.log.log(f"抓取到 {len(out)} 个子文档节点。")
        return out

    async def eval_list(self, page, js):
        try:
            return await page.evaluate(js) or []
        except Exception:
            return []


def load_links_file(path):
    """读 links.txt 里的手工备用清单。"""
    links = []
    if not path.exists():
        return links
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            links.append(line)
    return links
