#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探查导出接口：摸清 file_extension 认哪些值、评论/附件选项叫什么字段。

背景：飞书**网页版**的导出菜单里有 Word / PDF / Markdown 三种格式，各自还带
选项（Word/PDF：带不带评论；Markdown：仅正文 / 带附件的全部内容）。但这些都是
内部接口，字段名没有公开文档 —— 猜字段名只会白改一轮，所以先实测。

用法:
  uv run python probe_export.py            # 只探接口（提交任务，不下载）
  uv run python probe_export.py --download # 顺便把产物下下来看扩展名/内容

只读探查：提交的是导出任务，不会修改飞书上的任何内容。
"""

import asyncio
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from flybook import config as C  # noqa: E402
from flybook import session as S  # noqa: E402
from flybook.api import ApiError, FeishuApi, token_of  # noqa: E402

CONFIG_PATH = BASE / "config.json"
SESSION_PATH = BASE / S.SESSION_FILE


async def submit(api, label, payload):
    """提交一次导出任务，把**原始响应**打出来（不走 _unwrap，要看 code/msg）。"""
    url = f"{api.host}/space/api/export/create/"
    try:
        body = await api._call(url, "POST", payload)
    except ApiError as e:
        print(f"  ✗ {label:<22} 请求失败：{e}")
        return None
    code, msg = body.get("code"), body.get("msg")
    ticket = (body.get("data") or {}).get("ticket") or ""
    ok = code in (0, None) and ticket
    print(f"  {'✓' if ok else '✗'} {label:<22} code={code} msg={msg!r} ticket={ticket or '-'}")
    return ticket or None


async def settle(api, ticket, obj_token, label, download_dir=None):
    """轮询到出结果，报告 file_token 与（可选的）真实下载后的文件名/大小。"""
    try:
        file_token = await api.wait_export(
            ticket, obj_token, interval=1.5, timeout=90
        )
    except ApiError as e:
        print(f"      {label}: 导出未成功 —— {e}")
        return
    print(f"      {label}: file_token={file_token}")
    if not download_dir:
        return
    dst = Path(download_dir) / f"_probe_{label.replace(' ', '_').replace('/', '-')}.bin"
    try:
        size = await api.download(file_token, dst)
    except Exception as e:
        print(f"      {label}: 下载失败 —— {e}")
        return
    head = dst.read_bytes()[:64]
    kind = "?"
    if head.startswith(b"%PDF"):
        kind = "PDF"
    elif head[:2] == b"PK":
        kind = "ZIP 容器（docx/xlsx 这类）"
    else:
        try:
            head.decode("utf-8")
            kind = "纯文本（很可能是 markdown）"
        except UnicodeDecodeError:
            kind = "未知二进制"
    print(f"      {label}: 下载 {size} 字节，文件头判断 = {kind}")
    print(f"      {label}: 前 120 字节 = {head[:120]!r}")


class _Log:
    def log(self, *a):
        print(" ", *a)


async def probe_web(cfg):
    """把前端 JS 里构造 export/create 请求的那段代码挖出来。

    为什么要这么绕：这个接口对**任何**字段名都回 code=0，靠「试字段名 + 看返回码」
    根本分不出对错。而拿文档做 A/B 对比也不行 —— 首页既没评论也没附件，
    带不带那两个选项产物一模一样。只有前端自己的代码是权威的。
    """
    from playwright.async_api import async_playwright

    from flybook.browser import BrowserSession

    root_url = cfg["root_url"]
    async with async_playwright() as p:
        session = BrowserSession(cfg, _Log(), BASE / ".browser_profile",
                                 CONFIG_PATH, headless=False)
        await session.start(p)
        try:
            page = await session.first_page()
            print("打开文档页，等前端把 JS chunk 都拉下来…")
            await page.goto(root_url, wait_until="domcontentloaded",
                            timeout=cfg["nav_timeout_ms"])
            await asyncio.sleep(8)

            urls = await page.evaluate(
                "() => performance.getEntriesByType('resource')"
                ".map(e => e.name).filter(n => n.endsWith('.js'))"
            )
            print(f"页面上加载了 {len(urls)} 个 JS 文件，开始翻…\n")
            out = BASE / "_probe_js"
            out.mkdir(exist_ok=True)
            for u in urls:
                try:
                    text = await page.evaluate(
                        "u => fetch(u).then(r => r.text()).catch(() => '')", u)
                except Exception:
                    continue
                if not text:
                    continue
                (out / u.rsplit("/", 1)[-1]).write_text(text, encoding="utf-8")
                _report_hits(u, text)
            print(f"\n共保存到 {out}（看完可以整个删掉），之后可以直接 grep。")
        finally:
            await session.close()


# 要找的字段名。前两个是已知的，后面几个是猜的 —— 猜中哪个由 JS 说了算。
NEEDLES = ("file_extension", "need_comment", "attachment", "markdown", "with_comment")


def _report_hits(url, text):
    """在 JS 里找关键字段，打印它周围的片段（context 才看得出字段怎么用的）。"""
    hits = [(n, text.find(n)) for n in NEEDLES]
    hits = [(n, i) for n, i in hits if i >= 0]
    if not hits:
        return
    print(f"--- {url.rsplit('/', 1)[-1]} （{len(text)} 字节） ---")
    for name, idx in hits:
        for at in _all_positions(text, name, limit=3):
            lo, hi = max(0, at - 160), min(len(text), at + 160)
            print(f"  [{name}] …{text[lo:hi]}…")
        print()


def _all_positions(text, needle, limit=3):
    out, start = [], 0
    while len(out) < limit:
        i = text.find(needle, start)
        if i < 0:
            break
        out.append(i)
        start = i + 1
    return out


async def probe_capture(cfg):
    """实录：主人点一遍导出菜单，把真正发出去的请求原样录下来。

    为什么非得这样：export/create 对**任何**字段名都回 code=0，
    「试字段名 + 看返回码」分不出对错；拿文档做 A/B 也不行（首页既没评论
    又没附件，带不带那几个选项产物一模一样）。前端 JS 也没用 —— 构造请求的
    代码在懒加载的对话框 chunk 里，不点开就不会下载。

    所以只剩一条可靠的路：让浏览器自己发一次，我们照抄。
    """
    import json

    from playwright.async_api import async_playwright

    from flybook.browser import BrowserSession

    hits = []

    def on_request(req):
        low = req.url.lower()
        if "export" not in low:
            return
        body = None
        try:
            body = req.post_data
        except Exception:
            pass
        hits.append((req.method, req.url, body))
        print(f"\n>>> {req.method} {req.url.split('?')[0]}")
        if body:
            try:
                print("    " + json.dumps(json.loads(body), ensure_ascii=False))
            except Exception:
                print("    " + str(body)[:400])

    async with async_playwright() as p:
        session = BrowserSession(cfg, _Log(), BASE / ".browser_profile",
                                 CONFIG_PATH, headless=False)
        await session.start(p)
        try:
            page = await session.first_page()
            page.on("request", on_request)
            await page.goto(cfg["root_url"], wait_until="domcontentloaded",
                            timeout=cfg["nav_timeout_ms"])
            await asyncio.sleep(5)
            print(
                "\n" + "=" * 62 + "\n"
                "请在**浏览器窗口**里依次操作（每步做完停一下，让请求发出去）：\n"
                "\n"
                "  1. 打开导出菜单 → Markdown → 「所有内容」→ 确定\n"
                "  2. 再导一次  → Markdown → 「仅文本（不含图片和附件）」→ 确定\n"
                "  3. 再导一次  → Word     → 「仅正文」→ 确定\n"
                "  4. 再导一次  → Word     → 「导出正文及评论」→ 确定\n"
                "  5. 再导一次  → PDF      → 「仅正文」→ 确定\n"
                "  6. 再导一次  → PDF      → 「导出正文及评论」→ 确定\n"
                "\n"
                "每发一个请求，这里会立刻打印出来。\n"
                "全部做完后回到这个窗口按**回车**结束。\n" + "=" * 62
            )
            await asyncio.to_thread(input, "")
            # 顺手把这期间新加载的 chunk 也存下来，之后能离线 grep
            urls = await page.evaluate(
                "() => performance.getEntriesByType('resource')"
                ".map(e => e.name).filter(n => n.endsWith('.js'))"
            )
            out = BASE / "_probe_js"
            out.mkdir(exist_ok=True)
            new = 0
            for u in urls:
                dst = out / u.rsplit("/", 1)[-1]
                if dst.exists():
                    continue
                try:
                    text = await page.evaluate(
                        "u => fetch(u).then(r => r.text()).catch(() => '')", u)
                except Exception:
                    continue
                if text:
                    dst.write_text(text, encoding="utf-8")
                    new += 1
            print(f"\n另外存下 {new} 个新 chunk（对话框的代码应该就在里面）。")
        finally:
            await session.close()

    print(f"\n共录到 {len(hits)} 个 export 相关请求。")
    return 0


async def _visible_items(page):
    """当前页面上可见的、像菜单项/按钮的东西（用来判断上一步点出了什么）。"""
    return await page.evaluate(
        """() => {
            const out = [];
            const sel = '[role=menuitem],[role=option],button,li,[class*=menu-item],[class*=MenuItem]';
            for (const el of document.querySelectorAll(sel)) {
                const r = el.getBoundingClientRect();
                if (r.width < 4 || r.height < 4) continue;
                const t = (el.innerText || '').trim().replace(/\\s+/g, ' ');
                if (t && t.length <= 24) out.push(t);
            }
            return [...new Set(out)].slice(0, 40);
        }"""
    )


async def _click_text(page, text):
    """按可见文字点；`@x,y` 则按坐标点（顶栏那些纯图标按钮没文字，只能点坐标）。"""
    if text.startswith("@"):
        x, y = (int(v) for v in text[1:].split(","))
        try:
            await page.mouse.click(x, y)
            return True
        except Exception:
            return False
    # 先试**精确**匹配（text="xxx" 带引号才是精确），再退回模糊。
    # 踩过：模糊匹配下 `导出` 会先命中对话框标题「导出 Markdown 设置」，
    # 于是按钮根本没被点到，却一路报「✓ 点到了」。
    for sel in (f'text="{text}"', f"text={text}", f"[aria-label='{text}']"):
        try:
            loc = page.locator(sel).first
            if await loc.count():
                await loc.click(timeout=4000)
                return True
        except Exception:
            continue
    return False


async def probe_ui(cfg, clicks):
    """自己把导出菜单点一遍 —— 免去主人手动操作。

    只点不填：每一步都是「点开菜单 → 选一项 → 确定」，不碰文档内容。
    """
    import json

    from playwright.async_api import async_playwright

    from flybook.browser import BrowserSession

    hits = []

    def on_request(req):
        if "export" not in req.url.lower():
            return
        body = None
        try:
            body = req.post_data
        except Exception:
            pass
        hits.append((req.method, req.url, body))
        print(f"\n>>> {req.method} {req.url.split('?')[0]}")
        if body:
            try:
                print("    " + json.dumps(json.loads(body), ensure_ascii=False))
            except Exception:
                print("    " + str(body)[:400])

    async with async_playwright() as p:
        session = BrowserSession(cfg, _Log(), BASE / ".browser_profile",
                                 CONFIG_PATH, headless=False)
        await session.start(p)
        try:
            page = await session.first_page()
            page.on("request", on_request)
            await page.goto(cfg["root_url"], wait_until="domcontentloaded",
                            timeout=cfg["nav_timeout_ms"])
            await asyncio.sleep(6)

            for i, spec in enumerate(clicks, 1):
                print(f"\n--- 第 {i} 步：点「{spec}」---")
                ok = await _click_text(page, spec)
                print("    " + ("✓ 点到了" if ok else "✗ 页面上没找到这个文字"))
                await asyncio.sleep(3)
                items = await _visible_items(page)
                print(f"    现在可见的项：{items}")

            print(f"\n共录到 {len(hits)} 个 export 相关请求。")
            await asyncio.sleep(4)
            urls = await page.evaluate(
                "() => performance.getEntriesByType('resource')"
                ".map(e => e.name).filter(n => n.endsWith('.js'))"
            )
            out = BASE / "_probe_js"
            out.mkdir(exist_ok=True)
            new = 0
            for u in urls:
                dst = out / u.rsplit("/", 1)[-1]
                if dst.exists():
                    continue
                try:
                    text = await page.evaluate(
                        "u => fetch(u).then(r => r.text()).catch(() => '')", u)
                except Exception:
                    continue
                if text:
                    dst.write_text(text, encoding="utf-8")
                    new += 1
            print(f"另外存下 {new} 个新 chunk。")
        finally:
            await session.close()
    return 0


async def probe_dump(cfg):
    """把所有带文字提示的控件列出来，找「更多 / 导出」这类入口。"""
    from playwright.async_api import async_playwright

    from flybook.browser import BrowserSession

    async with async_playwright() as p:
        session = BrowserSession(cfg, _Log(), BASE / ".browser_profile",
                                 CONFIG_PATH, headless=False)
        await session.start(p)
        try:
            page = await session.first_page()
            await page.goto(cfg["root_url"], wait_until="domcontentloaded",
                            timeout=cfg["nav_timeout_ms"])
            await asyncio.sleep(6)
            rows = await page.evaluate(
                """() => {
                    const out = [];
                    for (const el of document.querySelectorAll('*')) {
                        const r = el.getBoundingClientRect();
                        if (r.width < 8 || r.height < 8) continue;
                        const st = getComputedStyle(el);
                        if (st.cursor !== 'pointer') continue;
                        if (st.visibility === 'hidden' || st.display === 'none') continue;
                        // 只留「叶子」—— 里层有 pointer 元素的话外层只是容器
                        if (el.querySelector('*[style*=pointer]')) continue;
                        // 只关心顶栏（导出入口在那儿），y 大了就是正文和侧边栏
                        if (r.y > 60 || r.height > 60) continue;
                        out.push({
                            t: (el.innerText || '').trim().slice(0, 14),
                            tag: el.tagName,
                            cls: (el.className || '').toString().slice(0, 40),
                            x: Math.round(r.x), y: Math.round(r.y),
                            w: Math.round(r.width),
                        });
                    }
                    return out.slice(0, 60);
                }"""
            )
            print(f"共 {len(rows)} 个可点元素：\n")
            for r in rows:
                print(f"  ({r['x']:>4},{r['y']:>4}) w={r['w']:>4} {r['tag']:<7} "
                      f"text={r['t']!r:<18} cls={r['cls']}")
        finally:
            await session.close()
    return 0


async def main():
    cfg = C.load(CONFIG_PATH)
    if "--dump" in sys.argv:
        await probe_dump(cfg)
        return 0
    if "--ui" in sys.argv:
        i = sys.argv.index("--ui")
        await probe_ui(cfg, sys.argv[i + 1:])
        return 0
    if "--capture" in sys.argv:
        await probe_capture(cfg)
        return 0
    if "--web" in sys.argv:
        await probe_web(cfg)
        return 0

    cached = S.load(SESSION_PATH)
    if not cached:
        print("没有缓存凭证，先跑一次 main.py 登录。")
        return 1
    root_url = cfg["root_url"]
    host = root_url.split("/wiki/")[0]
    api = FeishuApi(host, cached["cookie"], cached.get("csrf", ""),
                    referer=root_url, timeout=cfg["api_timeout"])

    node = await api.get_node(token_of(root_url))
    obj = str(node.get("obj_token") or "")
    print(f"用根文档试（obj_token 长度 {len(obj)}）\n")

    base = {"token": obj, "type": "docx", "event_source": "6"}

    # 一轮：格式值能不能被接受（每种都带 need_comment=False，跟现在线上行为一致）
    print("== 1. file_extension 认哪些值 ==")
    cases = [
        ("pdf", "pdf"),
        ("docx", "docx"),
        ("md", "md"),
        ("markdown", "markdown"),
    ]
    tickets = {}
    for label, ext in cases:
        t = await submit(api, f"{label}", {**base, "file_extension": ext,
                                           "need_comment": False})
        if t:
            tickets[label] = t

    print("\n== 2. 评论选项认不认（同一个格式提交两份，看产物是否有差异） ==")
    for ext in ("pdf", "docx"):
        for flag in (False, True):
            label = f"{ext} comment={flag}"
            t = await submit(api, label, {**base, "file_extension": ext,
                                          "need_comment": flag})
            if t:
                tickets[label] = t

    print("\n== 3. Markdown 的「带附件」可能叫什么 ==")
    for field in ("need_attachment", "with_attachment", "export_attachment",
                  "include_attachment"):
        t = await submit(api, f"md + {field}", {**base, "file_extension": "md",
                                                field: True, "need_comment": False})
        if t:
            tickets[f"md {field}"] = t

    if not tickets:
        print("\n一个都没提交成功 —— 多半是凭证过期，重跑一次 main.py。")
        return 1

    print("\n== 4. 等结果 ==")
    dl = str(BASE / "_probe_out") if "--download" in sys.argv else None
    if dl:
        Path(dl).mkdir(exist_ok=True)
    for label, ticket in tickets.items():
        await settle(api, ticket, obj, label, dl)

    if dl:
        print(f"\n产物在 {dl}（看完可以整个删掉）")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(asyncio.run(main()))
