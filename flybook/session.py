"""会话：cookie + csrf 的获取、缓存、刷新与**验收**。

只有这一步需要浏览器 —— 登录一次、打开根文档（顺带处理访问密码）、
把 cookie 和 `_csrf_token` 取下来，然后浏览器就退场了。
之后全部是纯 HTTP，不渲染任何页面。

⚠️ 取到 cookie ≠ 拿到了能用的凭证。这个知识库**凭访问密码就能匿名阅读**，
所以「浏览器里能看到正文」完全不能说明身份是合格的 —— 匿名身份照样能
打开单篇，只是 `get_tree` 会被拒，列不出整棵树，于是静默地少导出大半。
历史上一次 `--reset` 之后就是这么只导出了 15/41 篇，而且一声不吭。

所以 refresh() 现在多做一步**验收**：拿到 cookie 立刻真调一次 get_tree，
列不出来就等几秒重取重试，始终不行就带着原因返回 None（不再把匿名会话
当成功交出去）。

缓存文件含账号凭证，已加进 .gitignore，不要提交。
凭证失效时由上层捕获 ApiError 再调 refresh() 重来一次。
"""

import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlparse

from .api import ApiError, FeishuApi

SESSION_FILE = ".feishu_session.json"

VERIFY_TRIES = 3   # 验收最多试几次
VERIFY_WAIT = 3.0  # 两次验收之间等多久（登录后 cookie 生效有个时间差）


def load(path):
    """读缓存，坏了或不存在都返回 None（当作没有）。"""
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict) and data.get("cookie"):
        return data
    return None


def save(path, cookie, csrf, root_url=""):
    payload = {
        "cookie": cookie,
        "csrf": csrf,
        "root_url": root_url,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass
    return payload


def clear(path):
    try:
        Path(path).unlink()
    except Exception:
        pass


def join_cookies(jar):
    """把 Playwright 的 cookie 列表拼成请求头用的字符串，并单独摘出 csrf。"""
    cookie = "; ".join(f"{c['name']}={c['value']}" for c in jar)
    csrf = ""
    for c in jar:
        if c["name"] == "_csrf_token":
            csrf = c["value"]
            break
    return cookie, csrf


async def verify(api, root_token, logger, tries=VERIFY_TRIES, wait=VERIFY_WAIT):
    """拿真接口验收凭证：**能不能列出根文档下面的子节点**。

    这是唯一能把「匿名读者」和「知识库成员」分开的动作。匿名读者能 get_node
    打开单篇（凭访问密码），但 get_tree 会被拒 [920004004] PermFail ——
    而列整棵树恰恰是这条流水线的必经之路。

    返回 (True, "") 或 (False, 最后一次的失败原因)。
    """
    if not root_token:
        return False, "拿不到根文档 token"
    last = ""
    for i in range(1, tries + 1):
        try:
            await api.get_tree(root_token)
            return True, ""
        except ApiError as e:
            last = str(e)
            if i < tries:
                logger.log(f"  凭证验收未通过（第 {i}/{tries} 次）：{last}")
                await asyncio.sleep(wait)
    return False, last


def _diagnose(logger, reason):
    """验收失败时把「下一步该干嘛」说清楚，别只丢一个 PermFail。"""
    logger.log(f"✗ 凭证验收没通过：{reason}")
    logger.log("  这说明浏览器里的这个身份**列不出这个知识库的文档树**。常见原因：")
    logger.log("    1) 其实没登录 —— 知识库凭访问密码可以匿名看单篇，但列整棵树")
    logger.log("       要正式成员身份。请重跑下面这条命令，并在弹出的窗口里")
    logger.log("       **真的扫码登录**：")
    logger.log("         uv run python main.py --reset")
    logger.log("    2) 登录的账号不是这个知识库的成员（找主人要个权限）。")
    logger.log("    3) 接口本身变了 —— 跑 uv run python main.py --diag 看具体报错。")


async def refresh(cfg, logger, profile_dir, cfg_path, root_url, headless=False):
    """开浏览器登录一次，取回 cookie + csrf，并**当场验收**。失败返回 None。"""
    from playwright.async_api import async_playwright

    from .browser import BrowserSession

    logger.log("正在启动浏览器获取登录凭证…")
    u = urlparse(root_url)
    host = f"{u.scheme}://{u.netloc}"
    root_token = root_url.rstrip("/").rsplit("/", 1)[-1]

    async with async_playwright() as p:
        session = BrowserSession(cfg, logger, profile_dir, cfg_path, headless=headless)
        try:
            await session.start(p)
            page = await session.first_page()
            if not await session.ensure_login(page, root_url):
                return None
            if not await session.open_doc(page, root_url):
                logger.log("打不开根文档，拿不到有效凭证。")
                return None

            cookie, csrf = join_cookies(await session.context.cookies())
            if not cookie:
                logger.log("没取到任何 cookie。")
                return None
            api = FeishuApi(
                host, cookie, csrf, referer=root_url,
                timeout=cfg.get("api_timeout", 60),
            )
            # 验收放在浏览器还开着的时候做 —— 万一不过，人还在跟前，能直接重登。
            ok, reason = await verify(api, root_token, logger)
            if not ok:
                _diagnose(logger, reason)
                return None
            logger.log(
                f"凭证已取到并验收通过"
                f"（cookie {len(cookie)} 字符，csrf {'有' if csrf else '没有'}）。"
            )
            return {"cookie": cookie, "csrf": csrf}
        except Exception as e:
            logger.log(f"获取凭证失败：{e}")
            return None
        finally:
            await session.close()
