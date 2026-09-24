#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自检：不联网、不开浏览器，只验证纯逻辑模块。

用法: uv run python selftest.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from flybook import config as C
from flybook.exporter import Exporter, extract_zip, inspect_output, inspect_pdf, is_zip
from flybook.manifest import Manifest, opts_sig, safe_name, undedup

PASS, FAIL = [], []


def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}{('  ' + extra) if extra else ''}")


print("== 1. 配置加载（老 config.json 必须能直接跑） ==")
cfg = C.load(Path("config.json"))
check("读到 root_url", cfg["root_url"].startswith("https://"), cfg["root_url"][:40] + "…")
check("缺失键补齐默认值", cfg["concurrency"] == 4 and cfg["max_retry"] == 3)
check("skip_existing 默认开", cfg["skip_existing"] is True)
# 这条以前是查真实 config.json 里有没有 sidebar_xpath，太脆了 ——
# 一旦主人把那个废弃键清理掉，测试就红。改成拿临时配置来验行为本身。
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "config.json"
    p.write_text(
        '{"root_url": "https://x/y", "sidebar_xpath": "/html/body", "concurrency": "8"}',
        encoding="utf-8",
    )
    old = C.load(p)
    check("老配置里的未知键原样保留（不崩）", old.get("sidebar_xpath") == "/html/body")
    check("老配置里认识的键照常生效", old["concurrency"] == 8)
    check("老配置缺失的键补默认值", old["poll_interval"] == 2.0 and old["browser"] == "auto")

print("== 2. 配置类型校正（坏值回落默认） ==")
bad = C.coerce(
    {"concurrency": "abc", "retry_backoff": "x", "skip_existing": "false", "max_retry": "5"}
)
check("坏 int 回落", bad["concurrency"] == 4)
check("坏 float 回落", bad["retry_backoff"] == 2.0)
check("字符串布尔被识别", bad["skip_existing"] is False)
check("合法字符串被转换", bad["max_retry"] == 5)

print("== 3. 文件名安全化 ==")
check("非法字符替换", safe_name('a/b\\c:d*e?f"g<h>i|j') == "a_b_c_d_e_f_g_h_i_j")
check("末尾点与空格去掉", safe_name("标题... ") == "标题")
check("超长截断", len(safe_name("长" * 300, 120)) == 120)
check("空标题兜底", safe_name("").startswith("doc_"))

print("== 4. 增量清单与文件名分配 ==")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    m = Manifest(d / "manifest.json", d)  # 全新清单，没有基线

    p1 = m.allocate_path("文档A", "tokA", 120)
    check("首次分配用原名", p1.name == "文档A.pdf")

    # 同一 token 重新导出：复用原名，不产生 _2
    p1b = m.allocate_path("文档A", "tokA", 120)
    check("同 token 复用原名（消除 _2 堆积）", p1b.name == "文档A.pdf")

    # 不同 token 同标题：加序号（两个 worker 都还没写盘时也必须区分开）
    p2 = m.allocate_path("文档A", "tokB", 120)
    check("异 token 同标题加序号（并发预留生效）", p2.name == "文档A_2.pdf")

    # 无基线（清单不存在）时复用文件名覆盖，避免全量重跑堆出一片 _2
    (d / "文档D.pdf").write_text("上次的旧文件", encoding="utf-8")
    p5 = m.allocate_path("文档D", "tokD", 120)
    check("无基线时复用文件名覆盖", p5.name == "文档D.pdf")

    # record_ok / is_done
    real = d / "文档A.pdf"
    real.write_bytes(b"x" * 100)
    m.record_ok("tokA", "文档A", real, 3, 200)
    check("记录成功后 is_done 为真", m.is_done("tokA") is True)
    check("未记录的 token 为假", m.is_done("tokZ") is False)
    check("skip_existing=False 时不跳过", m.is_done("tokA", False) is False)

    real.write_bytes(b"x" * 999)  # 文件被外部改动
    check("文件大小变了就不再跳过", m.is_done("tokA") is False)

    m.record_fail("tokF", "失败文档", "PDF 校验未通过：文件过小")
    check("失败状态不会被跳过", m.is_done("tokF") is False)
    check("失败原因被记录", "文件过小" in m.docs["tokF"]["error"])

    m.save()
    m2 = Manifest(d / "manifest.json", d)
    check("清单可持久化并重新读回", len(m2.docs) == 2)

    # 有基线时：磁盘上的外来同名文件不被覆盖
    (d / "文档E.pdf").write_text("主人自己放的文件", encoding="utf-8")
    p6 = m2.allocate_path("文档E", "tokE", 120)
    check("有基线时外来同名文件不被覆盖", p6.name == "文档E_2.pdf")

print("== 5. PDF 校验（假成功识别） ==")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)

    missing = d / "不存在.pdf"
    r = inspect_pdf(missing)
    check("文件不存在被判失败", not r["ok"] and r["reason"] == "文件不存在")

    tiny = d / "过小.pdf"
    tiny.write_bytes(b"x" * 100)
    r = inspect_pdf(tiny)
    check("文件过小被判失败", not r["ok"] and "过小" in r["reason"])

    blank = d / "空白.pdf"
    blank.write_bytes(b"%PDF-1.4\n" + b"0" * 5000)
    r = inspect_pdf(blank)
    check("坏 PDF 被判失败", not r["ok"] and "解析失败" in r["reason"])

    # 造一个页数正常、体积够大、但没有任何文字的真 PDF
    # —— 这正是旧版会记成「成功」的假成功产物
    from pypdf import PdfWriter

    blank2 = d / "空白但够大.pdf"
    w = PdfWriter()
    w.add_blank_page(width=595, height=842)
    with open(blank2, "wb") as f:
        w.write(f)
    r = inspect_pdf(blank2, min_chars=50, min_bytes=100)
    check(
        "有页数无文字的 PDF 被判失败（假成功识别）",
        not r["ok"] and "文字过少" in r["reason"],
        r["reason"],
    )
    check("页数被正确读出", r["pages"] == 1)

    # 阈值可关：主人若遇到字体抽不出文字，可把 min_pdf_chars 设为 0
    r = inspect_pdf(blank2, min_chars=0, min_bytes=100)
    check("min_pdf_chars=0 时关闭文字校验", r["ok"] is True, r["reason"])

print("== 6. API 层纯逻辑 ==")
from flybook.api import ApiError, FeishuApi, dig  # noqa: E402

check("dig 按路径取值", dig({"a": {"b": {"c": 1}}}, "a", "b", "c") == 1)
check("dig 中途断了返回 None", dig({"a": 1}, "a", "b") is None)

api = FeishuApi("https://x.feishu.cn", "k=v", "CSRFVALUE")
h = api._headers({"t": 1})
# 这条是最贵的教训：header 写成 x-csrf-token 会收到 403 "csrf token error"，
# 看起来像没权限，其实只是名字多了一个连字符。
check("鉴权头名是 x-csrftoken", "x-csrftoken" in h)
check("没有误写成 x-csrf-token", "x-csrf-token" not in h)
check("POST 带 Content-Type", h.get("Content-Type") == "application/json")
check("GET 不带 Content-Type", "Content-Type" not in api._headers())

try:
    FeishuApi._unwrap({"code": 1014, "msg": "export file token not found"}, "导出")
    check("业务错误被识别（HTTP 200 但 code!=0）", False)
except ApiError as e:
    check("业务错误被识别（HTTP 200 但 code!=0）", "1014" in str(e), str(e)[:44])
check(
    "成功响应正常解包",
    FeishuApi._unwrap({"code": 0, "data": {"ticket": "abc"}}, "提交") == {"ticket": "abc"},
)

print("== 7. 会话缓存 ==")
from flybook import session as S  # noqa: E402

with tempfile.TemporaryDirectory() as td:
    sp = Path(td) / ".feishu_session.json"
    check("缓存不存在时返回 None", S.load(sp) is None)
    S.save(sp, "a=b; c=d", "CSRF", "https://x/wiki/yyy")
    got = S.load(sp)
    check(
        "缓存可读回",
        bool(got) and got["cookie"] == "a=b; c=d" and got["csrf"] == "CSRF",
    )
    sp.write_text("{坏掉的 json", encoding="utf-8")
    check("缓存损坏时当作没有（不崩）", S.load(sp) is None)
    S.save(sp, "x", "y")
    S.clear(sp)
    check("clear 后读不到", S.load(sp) is None)

print("== 8. 命名规则（特殊字符 → 下划线） ==")
# 主人点名的例子，原样对照
check(
    "顿号变下划线",
    safe_name("第1章-Java基础-基础语法、流程控制、方法")
    == "第1章-Java基础-基础语法_流程控制_方法",
    safe_name("第1章-Java基础-基础语法、流程控制、方法"),
)
check("& 变下划线", safe_name("Array、String&StringBuilder") == "Array_String_StringBuilder")
check("序号后的点变下划线并吞掉空格", safe_name("6. 接口文档-线索管理") == "6_接口文档-线索管理")
check("百分号变下划线（批处理里也安全）", safe_name("进度100%完成") == "进度100_完成")
check("全角冒号也换", safe_name("注意：重要") == "注意_重要")
check("全角引号也换", safe_name("“引用”内容") == "引用_内容")
# 这几组是刻意保留的：中文标题里太常见，换了反而难认
check("【】保留（不做替换）", safe_name("【重要】说明") == "【重要】说明")
check("《》保留（不做替换）", safe_name("《手册》第一章") == "《手册》第一章")
check("连字符保留（本来就是分隔符）", safe_name("第5章-Stream") == "第5章-Stream")
# 边界
check("连续特殊字符不会连出一串下划线", safe_name("a、、、b") == "a_b")
check("Windows 保留设备名被兜住", safe_name("CON") == "CON_")
check("首尾下划线与点被清掉", safe_name("..标题..") == "标题")

print("== 9. 目录结构（按父文档分层） ==")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    m = Manifest(d / "manifest.json", d)

    p = m.allocate_path("作业", "tokH", 120, ["第1章-Java基础-基础语法、流程控制、方法"])
    check("父标题做了同样的字符替换", p.parent.name == "第1章-Java基础-基础语法_流程控制_方法",
          p.parent.name)
    check("文件落在子目录下", p.name == "作业.pdf")
    check("目录被自动创建", p.parent.is_dir())

    p.write_bytes(b"x" * 10)
    m.record_ok("tokH", "作业", p, 1, 10)
    check("清单存的是相对路径（带子目录）", m.docs["tokH"]["file"].endswith("/作业.pdf"),
          m.docs["tokH"]["file"])
    check("带子目录的记录照样能判断跳过", m.is_done("tokH") is True)

    # 布局规则改过之后，老记录不能继续放行 —— 否则老文件还躺在老地方，
    # is_done 一路放行，文件纹丝不动，看着就像「改了没生效」。
    # 这些老记录没有 opts 字段，按扩展名推出来的历史默认签名来认。
    PDF_SIG = opts_sig("pdf")
    check(
        "位置没变时 is_current 为真",
        m.is_current("tokH", ["第1章-Java基础-基础语法、流程控制、方法"], PDF_SIG) is True,
    )
    check("该挪到顶层时立即判为假", m.is_current("tokH", [], PDF_SIG) is False)
    check(
        "该挪到别的目录时立即判为假",
        m.is_current("tokH", ["第2章-面向对象-封装、继承、静态"], PDF_SIG) is False,
    )
    check("没记录过的 token 一律为假", m.is_current("tokZ", [], PDF_SIG) is False)

    # 顶层文档记的是纯文件名，目录部分要能和「无父级」对上
    top = m.allocate_path("首页", "tokR", 120)
    top.write_bytes(b"y" * 10)
    m.record_ok("tokR", "首页", top, 1, 10)
    check("顶层文档的位置判断也对得上", m.is_current("tokR", [], PDF_SIG) is True)
    check("顶层文档被要求挪进新目录时判为假", m.is_current("tokR", ["新章节"], PDF_SIG) is False)

    # 不同章节下的同名文档，各进各的目录，互不打架
    p2 = m.allocate_path("作业", "tokH2", 120, ["第2章-面向对象-封装、继承、静态"])
    check("别的章节下同名文档不冲突", p2.parent.name == "第2章-面向对象-封装_继承_静态")
    check("顶层文档不受影响", m.allocate_path("首页", "tokR", 120).name == "首页.pdf")

print("== 10. 递归抓取（BFS，假 api 离线验） ==")
import asyncio  # noqa: E402

from flybook.collector import build_paths, probe_via_api  # noqa: E402


class _FakeApi:
    """按 parent 关系拼一棵假树，专门验 BFS 有没有漏层。

    broken 里的 token 会被模拟成「接口报错」（比如匿名身份的 PermFail），
    用来验错误该往上抛还是该被就地跳过。
    """

    def __init__(self, tree, broken=()):
        self.tree = tree
        self.broken = set(broken)
        self.calls = []

    async def get_node(self, tok):
        return {"obj_token": f"obj_{tok}", "title": self.tree[tok]["title"]}

    async def get_tree(self, tok):
        self.calls.append(tok)
        if tok in self.broken:
            raise ApiError(f"查询文档树失败：[920004004] PermFail（{tok}）")
        kids = [
            {
                "wiki_token": c,
                "obj_token": f"obj_{c}",
                "title": self.tree[c]["title"],
                "parent_wiki_token": tok,
                "sort_id": self.tree[c].get("sort_id", 0),
            }
            for c in self.tree.get(tok, {}).get("children", [])
        ]
        return {"wiki_token": tok}, kids


class _Log:
    def log(self, *_a):
        pass


TREE = {
    "root": {"title": "首页", "children": ["a", "b"]},
    "a": {"title": "第1章", "children": ["a1", "a2"]},
    "a1": {"title": "作业", "children": ["a1x"]},
    "a2": {"title": "总结", "children": []},
    "a1x": {"title": "作业答案", "children": []},
    "b": {"title": "接口文档", "children": []},
}

fake = _FakeApi(TREE)
root_obj, nodes, root_title = asyncio.run(probe_via_api(fake, "root", _Log()))
got = [t for t, _, _, _ in nodes]
check("三层树全部抓到（不是只抓第一层）", len(nodes) == 5, f"抓到 {len(nodes)} 篇")
check("第一层在", "a" in got and "b" in got)
check("第二层在（老版本就是漏在这里）", "a1" in got and "a2" in got)
check("第三层也在", "a1x" in got)
# 根也要问一次 —— 不问它哪来的第一层子节点
check(
    "每个节点恰好问一次（不重复请求）",
    len(fake.calls) == 6
    and sorted(fake.calls) == sorted(["root", "a", "b", "a1", "a2", "a1x"]),
    str(fake.calls),
)
check("根文档标题单独回传", root_title == "首页")
check("根文档不在 nodes 里（避免重复）", "root" not in got)

# 目录规则：有子文档的文档折进「以自己命名的文件夹」，自己也待在里面；
# 没子文档的就是一个平铺的 PDF。根文档是特例（留顶层，进 nodes 的都不是根）。
paths = build_paths("root", nodes)
check("有子文档的文档折进同名文件夹（自己也进去）", paths["a"] == ["第1章"], str(paths["a"]))
check("孙辈排在父文档的文件夹里", paths["a1"] == ["第1章", "作业"], str(paths["a1"]))
check("曾孙继承完整目录链", paths["a1x"] == ["第1章", "作业"], str(paths["a1x"]))
check("没子文档的文档平铺（不给自己建文件夹）", paths["b"] == [], str(paths["b"]))
check("没子文档但父有文件夹时，跟父走", paths["a2"] == ["第1章"], str(paths["a2"]))

# 环：parent 互相指不能把程序转死
cycle = [("x", "", "X", "y"), ("y", "", "Y", "x")]
try:
    build_paths("root", cycle)
    check("parent 成环时不死循环", True)
except RecursionError:
    check("parent 成环时不死循环", False)

# 根节点列不出来 = 凭证/权限整体不合格，必须抛上去
# （静默跳过的话 nodes 会是空表，上层以为「底下没子文档」，只导一篇就收工）
try:
    asyncio.run(probe_via_api(_FakeApi(TREE, broken={"root"}), "root", _Log()))
    check("根节点列不出来时抛错，不静默收工", False)
except ApiError as e:
    check("根节点列不出来时抛错，不静默收工", "PermFail" in str(e), str(e)[:44])

# 但深层某个节点坏了，只跳过它那棵子树，别拖垮整轮。
# 注意 a1 自己还在 —— 它是被爹 a 列出来的，丢的是它**下面的** a1x。
_, nodes2, _ = asyncio.run(probe_via_api(_FakeApi(TREE, broken={"a1"}), "root", _Log()))
got2 = sorted(t for t, _, _, _ in nodes2)
check(
    "深层节点失败只跳过它那棵子树",
    got2 == ["a", "a1", "a2", "b"] and "a1x" not in got2,
    str(got2),
)

print("== 11. 后台 stdin 读取器 ==")
from flybook import stdinio  # noqa: E402

r = stdinio.StdinReader()
r._lines.append("第一行")
r._lines.append("第二行")
check("按顺序取到第一行", asyncio.run(r.readline(timeout=1.0)) == "第一行")
check("按顺序取到第二行", asyncio.run(r.readline(timeout=1.0)) == "第二行")

eof = stdinio.StdinReader()
eof._eof = True
check("stdin 关闭时返回 None（不阻塞）", asyncio.run(eof.readline(timeout=1.0)) is None)

idle = stdinio.StdinReader()
try:
    asyncio.run(idle.readline(timeout=0.2))
    check("等不到输入时按时超时", False)
except asyncio.TimeoutError:
    check("等不到输入时按时超时（密码赛跑靠它）", True)

check("单例每次拿到同一个读取器", stdinio.get_reader() is stdinio.get_reader())

print("== 12. 登录态判定与凭证验收 ==")
# 这一节盯的是一个真出过事的地方：知识库凭访问密码可以匿名阅读，未登录时
# 打开文档看起来完全正常，于是程序以为「已登录」，静默地只导出了 15/41 篇。
from flybook.browser import is_login_url  # noqa: E402

check(
    "被甩到飞书登录站 = 没登录",
    is_login_url("https://accounts.feishu.cn/accounts/page/login?app_id=1&redirect_uri=x"),
)
check("知识库文档页不算登录页", is_login_url("https://x.feishu.cn/wiki/AbCdEf") is False)
check("本租户的 drive 页不算登录页", is_login_url("https://x.feishu.cn/drive/home/") is False)
check("国际版登录站也认", is_login_url("https://accounts.larksuite.com/accounts/page/login"))
check("带端口仍认", is_login_url("https://accounts.feishu.cn:443/accounts/page/login"))
check("空串不炸", is_login_url("") is False)
# 判定用 endswith("." + h) 而不是 `h in host`，就是为了挡住这种域名
check(
    "伪装域名不算登录站",
    is_login_url("https://accounts.feishu.cn.钓鱼.com/accounts/page/login") is False,
)


class _TreeApi:
    """假接口：前 fail 次 get_tree 报 PermFail，之后放行。"""

    def __init__(self, fail):
        self.left = fail
        self.calls = 0

    async def get_tree(self, tok):
        self.calls += 1
        if self.left > 0:
            self.left -= 1
            raise ApiError("[920004004] PermFail")
        return {}, []


ok, why = asyncio.run(S.verify(_TreeApi(2), "root", _Log(), tries=3, wait=0))
check("凭证偶尔不过会重试到通过", ok is True and why == "")

bad = _TreeApi(99)
ok, why = asyncio.run(S.verify(bad, "root", _Log(), tries=3, wait=0))
check("匿名会话（get_tree 一直被拒）过不了验收", ok is False and "PermFail" in why)
check("验收最多只试 tries 次", bad.calls == 3, f"试了 {bad.calls} 次")

ok, why = asyncio.run(S.verify(_TreeApi(0), "", _Log(), tries=3, wait=0))
check("拿不到根 token 时不瞎试", ok is False and "token" in why)

ck, cs = S.join_cookies([
    {"name": "a", "value": "1"},
    {"name": "_csrf_token", "value": "CSRF"},
    {"name": "b", "value": "2"},
])
check("cookie 拼成请求头格式", ck == "a=1; _csrf_token=CSRF; b=2", ck)
check("csrf 被单独摘出来", cs == "CSRF")

print("== 13. 导出格式与选项 ==")
# 这一节的期望值全是**抓包**得来的，不是猜的。这个接口对任何字段名都回
# code=0，看返回码分不出对错 —— 所以必须把真实形状锁死在这里。
import json  # noqa: E402

check("格式别名归一（word）", C.norm_format("word") == "word")
check("格式别名归一（docx→word）", C.norm_format("docx") == "word")
check("格式别名归一（markdown→md）", C.norm_format("markdown") == "md")
check("格式别名归一（大小写/空格）", C.norm_format("  MD ") == "md")
check("不认识的格式退回 pdf（不崩）", C.norm_format("xyz") == "pdf")
check("Word 的扩展名是 docx", C.ext_of({"export_format": "word"}) == "docx")
check("Markdown 的扩展名是 md", C.ext_of({"export_format": "md"}) == "md")

cfg_o = C.coerce({"export_format": "docx", "export_comment": "true",
                  "export_attachment": "false"})
check("老配置里的 docx 被归一成 word", cfg_o["export_format"] == "word")
check("字符串布尔被识别（评论开）", cfg_o["export_comment"] is True)
check("字符串布尔被识别（附件关）", cfg_o["export_attachment"] is False)
check("默认值：不带评论", C.DEFAULTS["export_comment"] is False)
check("默认值：带附件", C.DEFAULTS["export_attachment"] is True)


class _RecApi(FeishuApi):
    """把发出去的 payload 留下来，专门验请求形状。"""

    def __init__(self):
        super().__init__("https://x.feishu.cn", "k=v", "CSRF")

    async def _call(self, url, method="GET", payload=None):
        self.sent = payload
        return {"code": 0, "data": {"ticket": "TICKET"}}


def _sent(**kw):
    a = _RecApi()
    asyncio.run(a.export_create("OBJ", **kw))
    return a.sent


p = _sent(ext="pdf")
check("PDF 默认不带评论", p["need_comment"] is False)
check("PDF 不发 passback", "passback" not in p)
check("source 类型照旧是 docx", p["type"] == "docx" and p["file_extension"] == "pdf")

check("PDF + 评论 → need_comment 为真", _sent(ext="pdf", need_comment=True)["need_comment"] is True)
check("Word + 评论 → need_comment 为真",
      _sent(ext="docx", need_comment=True)["need_comment"] is True)
check("Word 的 file_extension 是 docx", _sent(ext="docx")["file_extension"] == "docx")

# Markdown 那两档：秘密全在 passback，而且是个**JSON 字符串**
md_all = _sent(ext="md", include_file=True)
check("Markdown「所有内容」→ 发 passback", "passback" in md_all)
check("passback 里是 include_file: true",
      json.loads(md_all["passback"]) == {"include_file": True}, md_all.get("passback"))
check("passback 是字符串不是对象", isinstance(md_all["passback"], str))
# 「仅文本」前端是**整个键都不发**，不是发 false
md_text = _sent(ext="md", include_file=False)
check("Markdown「仅文本」→ 完全不发 passback", "passback" not in md_text)
# Markdown 的设置框里没有评论选项，实测发的一直是 false
check("Markdown 即使传了评论也不发 true",
      _sent(ext="md", need_comment=True)["need_comment"] is False)

# 签名：换选项必须能被发现，否则老文件会被静默跳过
check("带评论与不带评论签名不同",
      opts_sig("pdf", True) != opts_sig("pdf", False))
check("同选项签名一致", opts_sig("pdf", False) == opts_sig("pdf", False))
check("Markdown 的附件开关进签名",
      opts_sig("md", attachment=True) != opts_sig("md", attachment=False))
# 评论开关只对 Word/PDF 有意义，不该影响 Markdown 的签名
# （否则导 Markdown 时顺手调一下评论，会让 41 篇 PDF 全部重下）
check("评论开关不影响 Markdown 签名", opts_sig("md", True) == opts_sig("md", False))

print("== 14. 按格式校验产物（docx/md 不能套 PDF 那套） ==")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)

    md = d / "a.md"
    # newline="" 很关键：Windows 上 write_text 默认把 \n 转成 \r\n，
    # 字数就会莫名多出几笔，测试跟着红
    md.write_text("# 标题\n\n正文内容", encoding="utf-8", newline="")
    r = inspect_output(md, "md", min_bytes=10)
    check("Markdown 产物校验通过", r["ok"] is True, r["reason"])
    check("Markdown 字数被数出来", r["chars"] == 10, str(r["chars"]))

    empty = d / "empty.md"
    empty.write_text("   \n", encoding="utf-8")
    check("空 Markdown 判失败", inspect_output(empty, "md", min_bytes=2)["ok"] is False)

    notmd = d / "b.md"
    notmd.write_bytes(b"\xff\xfe\x00\x01" * 20)
    r = inspect_output(notmd, "md", min_bytes=10)
    check("不是文本的产物判失败", r["ok"] is False and "UTF-8" in r["reason"], r["reason"])

    docx = d / "a.docx"
    docx.write_bytes(b"PK\x03\x04" + b"\x00" * 200)
    r = inspect_output(docx, "docx", min_bytes=10)
    check("Word 产物（ZIP 头）校验通过", r["ok"] is True, r["reason"])

    fake = d / "b.docx"
    fake.write_bytes(b"this is not a zip at all" * 10)
    r = inspect_output(fake, "docx", min_bytes=10)
    check("不是 ZIP 的 Word 产物判失败", r["ok"] is False and "ZIP" in r["reason"], r["reason"])

    # 关键：docx/md 走 inspect_output 不能崩，也不能套 PDF 的页数逻辑
    check("docx/md 的页数记为 0（不是 PDF）",
          inspect_output(docx, "docx", min_bytes=10)["pages"] == 0)

    p = d / "a.pdf"
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=595, height=842)
    with open(p, "wb") as f:
        w.write(f)
    check("PDF 仍然走原来的 inspect_pdf",
          inspect_output(p, "pdf", min_bytes=100)["pages"] == 1)

print("== 15. Markdown 附件包（飞书给的是 zip，得自己解开） ==")
import zipfile  # noqa: E402


def make_zip(path, entries):
    """entries 是 (名字, 内容) 或 (名字, 内容, 是否符号链接)。"""
    with zipfile.ZipFile(path, "w") as z:
        for name, data, *rest in entries:
            if rest and rest[0]:
                zi = zipfile.ZipInfo(name)
                zi.external_attr = 0o120777 << 16   # S_IFLNK | 0777
                z.writestr(zi, data)
            else:
                z.writestr(name, data)
    return path


with tempfile.TemporaryDirectory() as td:
    d = Path(td)

    good = make_zip(d / "a.zip", [
        ("文档标题.md", "# 正文\n有内容"),
        ("图片和附件/image.png", b"\x89PNG" + b"\x00" * 50),
    ])
    check("zip 产物按文件头认得出来", is_zip(good) is True)
    plain = d / "b.md"
    plain.write_text("# 就是普通 md", encoding="utf-8")
    check("普通 md 不会被误判成 zip", is_zip(plain) is False)

    dst = d / "out"
    dst.mkdir()
    main = extract_zip(good, dst, "安全名")
    check("正文解出来并改名成文件夹名", main == dst / "安全名.md", main.name)
    check("附件目录一起解出来", (dst / "图片和附件" / "image.png").is_file())
    check("解出来的正文能过校验",
          inspect_output(main, "md", min_bytes=16)["ok"] is True)

    # zip slip：条目名往上跳，解压就会写到目标目录外面去 —— 老牌攻击面
    evil = make_zip(d / "evil.zip", [("../../坏东西.txt", "x")])
    try:
        extract_zip(evil, d / "out2", "x")
        check("含 .. 的条目被拒绝", False, "居然解压成功了")
    except RuntimeError as e:
        check("含 .. 的条目被拒绝", "上级跳转" in str(e), str(e))

    absz = make_zip(d / "abs.zip", [("/tmp/坏东西.txt", "x")])
    try:
        extract_zip(absz, d / "out3", "x")
        check("绝对路径条目被拒绝", False, "居然解压成功了")
    except RuntimeError as e:
        check("绝对路径条目被拒绝", "绝对路径" in str(e), str(e))

    # 符号链接条目：后面再来一条经过它写的，照样能写到解压目录外面
    linkz = make_zip(d / "link.zip", [("说明书.md", "x"), ("link", "target", True)])
    try:
        extract_zip(linkz, d / "out4", "x")
        check("符号链接条目被拒绝", False, "居然解压成功了")
    except RuntimeError as e:
        check("符号链接条目被拒绝", "符号链接" in str(e), str(e))

    nomd = make_zip(d / "nomd.zip", [("图片和附件/a.png", b"x")])
    try:
        extract_zip(nomd, d / "out5", "x")
        check("zip 里没有正文时明确报错", False, "居然解压成功了")
    except RuntimeError as e:
        check("zip 里没有正文时明确报错", "没找到" in str(e), str(e))

    # 467 字节是实测到的真实值：知识库根文档「首页」的正文就这么长。
    # 它卡在 PDF 门槛 3072 之下 —— 这就是那次 20 篇挂 19 篇的一半原因。
    short = d / "短文档.md"
    short.write_text("x" * 467, encoding="utf-8")
    check("467 字节的正常短文档按文本门槛放行",
          inspect_output(short, "md", min_bytes=16)["ok"] is True)
    check("同一个文件套 PDF 门槛会被误杀（这就是那个 bug）",
          inspect_output(short, "md", min_bytes=3072)["ok"] is False)

print("== 16. 带附件的 Markdown 折进自己的同名文件夹 ==")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    m = Manifest(d / "manifest.json", d)

    p = m.allocate_path("预习任务", "tokW", 120, ["Day01"], "md", wrap=True)
    check("折进自己的同名文件夹",
          p.parent.name == "预习任务" and p.name == "预习任务.md", p.as_posix())
    check("目录被自动创建", p.parent.is_dir())

    p.write_text("# 正文", encoding="utf-8")
    MD_ALL = opts_sig("md", attachment=True)
    m.record_ok("tokW", "预习任务", p, 0, 3, MD_ALL)
    check("包一层之后位置判断仍为真",
          m.is_current("tokW", ["Day01"], MD_ALL, wrap=True, title="预习任务") is True)
    # 形态（扁平 .md ↔ 同名文件夹）变了必须判假，否则又是一次静默跳过
    check("要求换成扁平形态时立刻判为假",
          m.is_current("tokW", ["Day01"], MD_ALL, wrap=False) is False)

    # 同名撞车：序号加在**文件夹**上，文件名跟着走
    p2 = m.allocate_path("预习任务", "tokW2", 120, ["Day01"], "md", wrap=True)
    check("同名文档的文件夹加序号",
          p2.parent.name == "预习任务_2" and p2.name == "预习任务_2.md", p2.as_posix())
    p2.write_text("# 另一篇", encoding="utf-8")
    m.record_ok("tokW2", "预习任务", p2, 0, 3, MD_ALL)
    # 关键：期望值算出来是「预习任务」，记录里却是「预习任务_2」——
    # 不剥去重尾巴就会永远判「位置不对」，于是每次运行都白重下一遍
    check("去重后缀不影响位置判断",
          m.is_current("tokW2", ["Day01"], MD_ALL, wrap=True, title="预习任务") is True)
    check("剥去重尾巴不会误伤正常标题", undedup("第1章") == "第1章" and
          undedup("作业_2") == "作业")

print("== 17. 导出器按格式选门槛与形态 ==")


class _NullLog:
    def log(self, *a, **k):
        pass


mdcfg = C.coerce({"export_format": "md", "export_attachment": True})
e = Exporter(None, None, mdcfg, _NullLog(), {})
check("Markdown 带附件 → 折文件夹", e.wrap is True)
check("Markdown 用文本门槛，不套 PDF 的",
      e._min_bytes() == mdcfg["min_text_bytes"] == 16)
check("日志里说人话的格式名", e.ext_label == "Markdown", e.ext_label)

e2 = Exporter(None, None, C.coerce({"export_format": "md", "export_attachment": False}),
              _NullLog(), {})
check("Markdown 仅文本 → 不折文件夹（单个 .md 没附件可撞名）", e2.wrap is False)

pdfcfg = C.coerce({"export_format": "pdf"})
e3 = Exporter(None, None, pdfcfg, _NullLog(), {})
check("PDF 不折文件夹", e3.wrap is False)
check("PDF 仍旧用 PDF 门槛", e3._min_bytes() == pdfcfg["min_pdf_bytes"] == 3072)
check("老配置里没有 min_text_bytes 时补默认值",
      C.coerce({"root_url": "https://x/y"})["min_text_bytes"] == 16)

print()
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项。")
for n in FAIL:
    print(f"  未通过: {n}")
sys.exit(1 if FAIL else 0)
