"""飞书网页版内部 API 客户端：文档树查询、导出任务、下载。

⚠️ 用的是飞书**网页版的内部接口**，不是开放平台 API，没有任何兼容性承诺，
随时可能变。所有接口都集中在本文件，将来失效了只改这一个地方。

鉴权 = Cookie + 请求头 `x-csrftoken`（注意：没有连字符分隔），
值取 cookie 里的 `_csrf_token`。
踩坑记录：写成 `x-csrf-token` 会收到 403 "csrf token error"，
看起来像权限不足，其实只是头名字错了。

导出是**异步任务**，四步走：
  1. POST export/create      提交，拿 ticket
  2. GET  export/result      轮询 job_status，成功后拿到 file_token
  3. GET  box/stream/download 下载成品
注意 node_token 不能直接导出（服务端返回 code 1014 "export file token
not found"），必须先用 get_node 换成文档本体的 obj_token。
"""

import asyncio
import gzip
import json
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 下载走的是另一个域名，和文档域不同
STREAM_HOST = "https://internal-api-drive-stream.feishu.cn"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# job_status：实测 2=处理中、0=成功；1=失败（沿用飞书开放平台的语义）
JOB_DONE, JOB_FAILED, JOB_RUNNING = 0, 1, 2


class ApiError(RuntimeError):
    """接口层错误。带业务 code 和响应片段，方便排查。"""

    def __init__(self, msg, code=None, body=""):
        super().__init__(msg)
        self.code = code
        self.body = body


def dig(obj, *keys):
    """按路径取嵌套字典的值，中间断了就返回 None。"""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def sort_key_of(node):
    """按 sort_id 排序用的键，保持文档在侧边栏里的原始顺序。

    sort_id 实测是很大的整数，但接口不保证；类型不对时退化为字符串比较，
    总之不能因为一个怪值就让整轮抓取崩掉。
    """
    v = node.get("sort_id") if isinstance(node, dict) else None
    try:
        return (0, int(v), "")
    except (TypeError, ValueError):
        return (1, 0, str(v or ""))


class FeishuApi:
    """一个租户域 + 一份凭证 = 一个客户端。线程安全（无共享可变状态）。"""

    def __init__(self, host, cookie, csrf="", referer="", timeout=60):
        self.host = host.rstrip("/")
        self.cookie = cookie
        self.csrf = csrf
        self.referer = referer or f"{self.host}/"
        self.timeout = timeout

    # ---------------- 底层 ----------------

    def _headers(self, payload=None):
        h = {
            "Cookie": self.cookie,
            "User-Agent": UA,
            "Referer": self.referer,
            "Accept": "application/json, text/plain, */*",
        }
        if self.csrf:
            h["x-csrftoken"] = self.csrf  # 没有连字符，写错就是 403
        if payload is not None:
            h["Content-Type"] = "application/json"
        return h

    def _request(self, url, method="GET", payload=None):
        """同步请求，返回解析后的 JSON。"""
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, headers=self._headers(payload), method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise ApiError(f"HTTP {e.code}", code=e.code, body=detail) from e
        except urllib.error.URLError as e:
            raise ApiError(f"网络错误：{e.reason}") from e

        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError("返回的不是 JSON（可能是登录页或接口已变更）",
                           body=raw[:300]) from None

    def _download_sync(self, file_token, dst):
        """同步下载到 dst，返回字节数。"""
        url = f"{STREAM_HOST}/space/api/box/stream/download/all/{file_token}/"
        req = urllib.request.Request(url, headers=self._headers())
        dst = Path(dst)
        with urllib.request.urlopen(req, timeout=max(self.timeout, 180)) as resp:
            enc = resp.headers.get("Content-Encoding", "")
            if "gzip" in enc:
                dst.write_bytes(gzip.decompress(resp.read()))
            else:
                with open(dst, "wb") as f:
                    shutil.copyfileobj(resp, f)
        return dst.stat().st_size

    @staticmethod
    def _unwrap(body, what):
        """飞书业务错误是 HTTP 200 + code!=0，得单独挑出来。"""
        code = body.get("code")
        if code not in (0, None):
            raise ApiError(
                f"{what}失败：[{code}] {body.get('msg') or '未知原因'}",
                code=code,
                body=json.dumps(body, ensure_ascii=False)[:300],
            )
        return body.get("data") or {}

    async def _call(self, url, method="GET", payload=None):
        return await asyncio.to_thread(self._request, url, method, payload)

    # ---------------- 文档树 ----------------

    async def get_node(self, wiki_token):
        """wiki 节点 → 文档本体信息。

        返回含 obj_token（导出要用的本体标识）和 space_id。
        响应实测嵌两层 data.data，这里做成能兼容两种深度。
        """
        url = (f"{self.host}/space/api/wiki/v2/tree/get_node/?"
               + urllib.parse.urlencode({"wiki_token": wiki_token}))
        data = self._unwrap(await self._call(url), "查询节点")
        inner = data
        for _ in range(3):
            if isinstance(inner, dict) and inner.get("obj_token"):
                break
            nxt = inner.get("data") if isinstance(inner, dict) else None
            if not isinstance(nxt, dict):
                break
            inner = nxt
        if not isinstance(inner, dict) or not inner.get("obj_token"):
            raise ApiError(f"节点 {wiki_token} 没返回 obj_token",
                           body=json.dumps(data, ensure_ascii=False)[:300])
        return inner

    async def get_tree(self, wiki_token):
        """拉取**一层**：本节点 + 它的直接子节点。返回 (本节点, [子节点, ...])。

        两个节点都是接口原始 dict，常用字段：
          wiki_token / obj_token / title / parent_wiki_token / has_child / sort_id

        ⚠️ 这个接口**不递归**，只给一层。曾经误以为「一次调用就能拿全库节点」，
        结果整个文档树只导出到第一层 —— 实测 41 篇的库只下来了 15 篇，漏掉 26 篇。
        子节点下面还有子节点时，必须拿子节点的 token 再问一次（见 collector 的
        BFS 遍历）。判断还有没有下一层看 has_child。

        踩坑记录：参数名必须是 **wiki_token**。传 space_id（哪怕值是对的、
        浏览器里也用同一个值）会得到 [920004004] PermFail —— 看着像没权限，
        其实只是参数名不对。空 space_id 反而返回别的空间，更容易带偏。
        """
        url = (f"{self.host}/space/api/wiki/v2/tree/get_info/?"
               + urllib.parse.urlencode({
                   "wiki_token": wiki_token, "with_space": "true", "with_perm": "t",
               }))
        data = self._unwrap(await self._call(url), "查询文档树")

        nodes, cur = None, data
        while isinstance(cur, dict) and nodes is None:
            nodes = dig(cur, "tree", "nodes")
            cur = cur.get("data")
        if not isinstance(nodes, dict):
            raise ApiError("文档树响应里找不到 tree.nodes",
                           body=json.dumps(data, ensure_ascii=False)[:300])

        self_node = nodes.get(wiki_token)
        # 只认「父节点正好是我」的那些，别的都是同 space 下的旁支，别顺手牵羊。
        children = [
            n for n in nodes.values()
            if isinstance(n, dict) and n.get("parent_wiki_token") == wiki_token
        ]
        children.sort(key=sort_key_of)
        return (self_node if isinstance(self_node, dict) else {}), children

    # ---------------- 导出任务 ----------------

    async def export_create(self, obj_token, ext="pdf", src_type="docx",
                            need_comment=False, include_file=None):
        """提交导出任务，返回 ticket。

        src_type 是**源文档类型**（新版文档=docx），ext 才是要转成的格式。

        ext: "pdf" / "docx"（Word）/ "md"（Markdown）

        need_comment: Word/PDF 的「导出正文及评论」。
            界面上这两个选项分别叫「仅正文」和「导出正文及评论」。

        include_file: Markdown 专用。True = 界面上的「所有内容」，
            False/None = 「仅文本（不含图片和附件）」。

        ⚠️ include_file 这两个字段是**实测抓包**得来的，不是猜的，别照直觉改：

        1. 它**不走独立字段**，而是塞在 `passback` 里；
        2. `passback` 的值是 **JSON 字符串**，不是 JSON 对象；
        3. 选「仅文本」时前端**根本不发 passback 这个键**，
           不是发 `{"include_file": false}` —— 所以这里也只在带附件时才加。

        这个接口对任何字段名都回 code=0，光看返回码分不出对错；而拿文档做 A/B
        也没用（没有评论/附件的文档，两种选项产物一模一样）。要重新确认，
        用 `probe_export.py --ui` 把真实请求录下来。
        """
        payload = {
            "token": obj_token,
            "type": src_type,
            "file_extension": ext,
            "event_source": "6",
            # Markdown 的设置对话框里**没有**评论这一项，实测发的就是 false，
            # 所以这个开关只对 Word/PDF 生效。
            "need_comment": bool(need_comment) and ext != "md",
        }
        if ext == "md" and include_file:
            payload["passback"] = json.dumps({"include_file": True})

        url = f"{self.host}/space/api/export/create/"
        body = await self._call(url, "POST", payload)
        ticket = str(self._unwrap(body, "提交导出任务").get("ticket") or "")
        if not ticket:
            raise ApiError("提交导出任务后没拿到 ticket")
        return ticket

    async def export_result(self, ticket, obj_token, src_type="docx"):
        """查一次导出进度，返回 result 字典。"""
        url = (f"{self.host}/space/api/export/result/{ticket}?"
               + urllib.parse.urlencode({"token": obj_token, "type": src_type}))
        return self._unwrap(await self._call(url), "查询导出进度").get("result") or {}

    async def wait_export(self, ticket, obj_token, interval=2.0, timeout=300,
                          src_type="docx", on_tick=None):
        """轮询到导出完成，返回 file_token。超时抛 ApiError。"""
        waited = 0.0
        last = None
        while waited < timeout:
            res = await self.export_result(ticket, obj_token, src_type)
            status = res.get("job_status")
            if status != last:
                last = status
            if status == JOB_DONE:
                token = str(res.get("file_token") or "")
                if not token:
                    raise ApiError("导出完成但没返回 file_token")
                return token
            if status == JOB_FAILED:
                raise ApiError(f"导出被服务端判失败："
                               f"{res.get('job_error_msg') or '未给原因'}")
            if on_tick:
                on_tick(res)
            await asyncio.sleep(interval)
            waited += interval
        raise ApiError(f"导出超时（{timeout:.0f} 秒仍在处理中）")

    async def download(self, file_token, dst):
        return await asyncio.to_thread(self._download_sync, file_token, dst)
