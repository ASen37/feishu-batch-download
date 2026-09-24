"""浏览器会话：只负责登录与取凭证。

改造后浏览器只干一件事 —— 登录一次、打开根文档（顺带处理访问密码）、
把 cookie 交给 session 模块，然后退场。

之前的「渲染等待、找滚动容器、滚动触发懒加载、page.pdf() 打印」整套都删了。
原因是那条路只能拿到**当前视口里已渲染的 DOM**：实测同一篇文档，打印出来是
1 页 852 字，而官方导出是 5 页 3150 字 —— 不是慢，是内容残缺。既然官方导出
走的是服务端渲染，能拿到数据库里的完整文档，就没必要再跟视口较劲。

保留的要点：
- 用 async API。sync API 不是线程安全的，同一个 persistent context 无法跨线程并发，
  而 user_data_dir 又被浏览器独占锁定，开不了第二个进程，所以并发只能走 asyncio。
- 访问密码仍在这里处理：打开文档时若弹出密码框就自动填，并写回 config.json。
  密码有两条路可走 —— 在终端里输，或者直接在浏览器窗口里输。两者**同时**盯着，
  谁先完成算谁的（靠 stdinio 的后台读取器实现，见 resolve_password）。
- 登录同理：检测到登录页就等，主人登录完自动继续，**不用回终端按回车**。

浏览器选择：config 里的 `browser` 可选 auto / chrome / msedge / custom / chromium。
auto（默认）按 **Chrome → Edge → 你指定的路径 → Playwright 自带内核** 依次尝试，
哪个起得来就用哪个，省得关心自己装了啥。custom 用 `browser_path` 指向的 exe。

⚠️ 用的是**独立的用户数据目录**，跟你平时用的浏览器不是同一套登录态 ——
「我 Chrome 里明明登录过飞书」不算数，得在弹出的那个窗口里再登一次。

profile 按浏览器分开存：Chrome 沿用旧版的根目录（升级后不用重登），
其余各用各的子目录，免得 profile 格式不兼容互相打架。
"""

import asyncio
import time
from urllib.parse import urlparse

from . import config as config_mod
from . import stdinio

# 候选浏览器与显示名。顺序即 auto 模式下的尝试顺序。
BROWSERS = ("chrome", "msedge", "custom", "chromium")
LABELS = {
    "chrome": "本机 Google Chrome",
    "msedge": "本机 Microsoft Edge",
    "custom": "自定义浏览器（browser_path）",
    "chromium": "Playwright 自带 Chromium",
}

# 飞书的登录站。未登录时，任何需要登录的页面都会被甩到这些域名上。
LOGIN_HOSTS = ("accounts.feishu.cn", "accounts.larksuite.com", "passport.feishu.cn")

# 用来**实测登录态**的页面：它必须登录才进得去，且不认访问密码。
# ⚠️ 千万别拿文档页当试金石 —— 这个知识库凭访问密码可以匿名阅读，
#    未登录时打开文档看起来完全正常（有正文、没有登录框）。
LOGIN_PROBE_PATH = "/drive/home/"


def is_login_url(url):
    """纯函数：这个地址是不是「被甩到登录站」了。

    只看域名，不看页面长相。抽成纯函数是为了能离线自检 —— 这个判定错了的
    代价很大（见 is_logged_in 的注释），值得单独锁死。

    注意用 `host == h or host.endswith("." + h)` 而不是 `h in host`：
    后者会把 `accounts.feishu.cn.钓鱼.com` 也认成登录站。
    """
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    host = host.split("@")[-1].split(":")[0]  # 去掉可能的 user:pass@ 和端口
    return any(host == h or host.endswith("." + h) for h in LOGIN_HOSTS)


class BrowserSession:
    def __init__(self, cfg, logger, profile_dir, cfg_path, headless=False):
        self.cfg = cfg
        self.log = logger
        self.profile_dir = profile_dir
        self.cfg_path = cfg_path
        self.headless = headless
        self.context = None
        self.browser_name = ""

    # ---------------- 选哪个浏览器 ----------------

    def _profile_dir(self, name):
        """每个浏览器用独立的 profile 目录。

        Chrome 沿用旧版的根目录，这样从 v0.3 升级上来不用重新登录；
        其他浏览器各用各的子目录，避免不同浏览器的 profile 格式互相打架。
        """
        if name == "chrome":
            return self.profile_dir
        return self.profile_dir / name

    def _candidates(self):
        """按顺序给出要尝试的浏览器，返回 [(名字, launch 参数)]。"""
        path = str(self.cfg.get("browser_path") or "").strip()
        pool = {
            "chrome": {"channel": "chrome"},
            "msedge": {"channel": "msedge"},
            "custom": {"executable_path": path} if path else None,
            "chromium": {},
        }
        want = str(self.cfg.get("browser") or "auto").strip().lower()
        if want == "auto":
            order = list(BROWSERS)
        elif want in pool:
            order = [want]
        else:
            self.log.log(f"browser 配置值「{want}」不认识，按 auto 处理。")
            order = list(BROWSERS)

        picks = [(n, pool[n]) for n in order if pool[n] is not None]
        if not picks:
            # 比如 browser=custom 却没填 browser_path。与其抛个空列表让上层
            # 撞 IndexError，不如退回全量候选，并说清原因。
            self.log.log(f"browser={want} 但没有可用的候选，按 auto 处理。")
            picks = [(n, pool[n]) for n in BROWSERS if pool[n] is not None]
        return picks

    # ---------------- 生命周期 ----------------

    async def start(self, playwright):
        tries = self._candidates()
        if not tries:
            raise RuntimeError("没有可用的浏览器候选，请检查 config.json 的 browser / browser_path")
        self.log.log("正在启动浏览器")
        self.log.log(f"  候选顺序：{' → '.join(LABELS[n] for n, _ in tries)}")
        self.log.log("  提示：若长时间无反应，请先关闭上次残留的浏览器窗口后重试。")
        last_err = None
        for name, extra in tries:
            label = LABELS[name]
            kwargs = dict(
                user_data_dir=str(self._profile_dir(name)),
                headless=self.headless,
                viewport={"width": 1440, "height": 900},
                **extra,
            )
            try:
                self.context = await playwright.chromium.launch_persistent_context(**kwargs)
            except Exception as e:
                last_err = e
                self.log.log(f"  ✗ {label} 起不来：{str(e)[:110]}")
                continue
            self.browser_name = name
            self.log.log(f"浏览器启动完成（{label}）。")
            return
        raise RuntimeError(
            f"没找到能用的浏览器，最后试的是 {LABELS[tries[-1][0]]}：{last_err}"
        )

    async def first_page(self):
        if self.context.pages:
            page = self.context.pages[0]
            page.set_default_timeout(self.cfg["page_timeout_ms"])
            return page
        page = await self.context.new_page()
        page.set_default_timeout(self.cfg["page_timeout_ms"])
        return page

    async def close(self):
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass

    # ---------------- 通用 JS ----------------

    async def eval_js(self, page, js, default=None, quiet=True):
        try:
            return await page.evaluate(js)
        except Exception as e:
            if not quiet:
                self.log.log(f"(JS 执行失败：{e})")
            return default

    # ---------------- 登录与密码 ----------------

    async def is_login_page(self, page):
        """当前页面是不是一张**登录表单**（不判断登没登录，只看长相）。"""
        if is_login_url(page.url) or "login" in page.url.lower():
            return True
        if await self.eval_js(page, "() => !!document.querySelector('input[type=tel]')"):
            return True
        if await self.eval_js(
            page, "() => (document.body.innerText||'').includes('扫码登录')"
        ):
            return True
        return False

    async def is_logged_in(self, page, root_url):
        """实测登录态。返回 True=已登录 / False=没登录 / None=探不出来。

        ⚠️ 为什么必须单独实测，而不是看文档页「有没有登录框」：

        这个知识库**凭访问密码就能匿名阅读**。未登录时打开文档页，正文照样
        显示、也没有登录框 —— 跟已登录长得一模一样。唯一的区别是匿名身份
        `get_tree` 会被拒（[920004004] PermFail），列不出整棵树。

        上一版就是栽在这里：`--reset` 清掉登录态后打开文档只看到密码框，
        判定「已进入文档页面（无需重新登录）」，于是拿着匿名会话往下跑，
        静默地只导出了 15/41 篇。

        所以换一个**不认密码、只认登录**的页面来试金石。
        """
        u = urlparse(root_url)
        probe = f"{u.scheme}://{u.netloc}{LOGIN_PROBE_PATH}"
        try:
            await page.goto(
                probe, wait_until="domcontentloaded",
                timeout=self.cfg["nav_timeout_ms"],
            )
        except Exception as e:
            self.log.log(f"  （探测登录态时页面打不开：{str(e)[:100]}）")
            return None
        await asyncio.sleep(1.5)
        return not is_login_url(page.url)

    async def ensure_login(self, page, root_url, timeout=300.0):
        """确认**真的登录了**；没登录就在浏览器窗口里等人登完，自动继续。

        刻意分两步，别合并成一个判断：
          1. 先打开根文档 —— 若它就是登录页，主人可以直接在这儿登；
          2. 再拿一个必须登录才能进的页面实测一次。
             第 1 步「没看到登录框」**不等于**「登录了」，理由见 is_logged_in。
        """
        self.log.log(f"打开根文档：{root_url}")
        for attempt in range(1, 4):
            try:
                await page.goto(
                    root_url,
                    wait_until="domcontentloaded",
                    timeout=self.cfg["nav_timeout_ms"],
                )
            except Exception as e:
                self.log.log(f"打开页面失败（第 {attempt} 次）：{e}")
            await asyncio.sleep(2.0)

            if await self.is_login_page(page):
                self.log.log(f"检测到登录页面（第 {attempt} 次），请在浏览器窗口里完成登录。")
                self.log.log("  → 登录完成后程序会自动继续，不用回终端按回车。")
                if not await self._wait_login_gone(page, timeout):
                    self.log.log(f"等待登录超时（{timeout:.0f} 秒）。")
                    return False
                self.log.log("登录页已消失，正在确认登录态…")

            state = await self.is_logged_in(page, root_url)
            if state is True:
                self.log.log("登录态确认：已登录。")
                return True
            if state is None:
                self.log.log("探测不出登录态（探测页打不开），按已登录继续。")
                return True

            # state is False —— 没登录。飞书这时通常已把浏览器停在登录页上了。
            self.log.log("⚠️  检测到**尚未登录**飞书账号。")
            self.log.log("    请在浏览器窗口里登录（扫码或手机号都行），登录完自动继续。")
            self.log.log("    为什么要登录：这个知识库凭访问密码可以匿名看单篇文档，")
            self.log.log("    但**列不出整棵文档树** —— 不登录就只会导出一部分。")
            if not await self._wait_logged_in(page, root_url, timeout):
                self.log.log(f"等待登录超时（{timeout:.0f} 秒）。")
                return False
            return True
        self.log.log("登录未完成，已放弃。")
        return False

    async def _wait_login_gone(self, page, timeout):
        """轮询到登录页消失为止。"""
        waited, since_log = 0.0, 0.0
        while waited < timeout:
            await asyncio.sleep(2.0)
            waited += 2.0
            since_log += 2.0
            if not await self.is_login_page(page):
                return True
            if since_log >= 30:
                since_log = 0.0
                self.log.log(f"  …仍在等待登录（已等 {int(waited)} 秒）")
        return False

    async def _wait_logged_in(self, page, root_url, timeout):
        """轮询到「真的登录了」为止。

        ⚠️ 这里**不能**每轮都重新导航到探测页：未登录时它会重定向回登录页，
        等于每隔两秒把页面刷一次，主人正在输的手机号会被清空。
        所以先只盯当前地址 —— 还在登录站上就是没登完，等它自己跳走。
        """
        waited, since_log = 0.0, 0.0
        while waited < timeout:
            await asyncio.sleep(2.0)
            waited += 2.0
            since_log += 2.0
            if not is_login_url(page.url):
                # 已经自己离开登录站了，这时再正式验一次（这次导航是安全的）
                if await self.is_logged_in(page, root_url) is not False:
                    self.log.log("登录成功。")
                    return True
            if since_log >= 30:
                since_log = 0.0
                self.log.log(f"  …仍在等待登录（已等 {int(waited)} 秒）")
        return False

    # ---------------- 访问密码 ----------------

    async def _has_password_box(self, page):
        return bool(
            await self.eval_js(page, "() => !!document.querySelector('input[type=password]')")
        )

    async def _wait_box_gone(self, page, timeout):
        """轮询到密码框消失（= 有人把密码输进去了）。"""
        waited = 0.0
        while waited < timeout:
            await asyncio.sleep(1.0)
            waited += 1.0
            if not await self._has_password_box(page):
                return True
        return False

    async def fill_password(self, page, pwd):
        """填写并提交页面访问密码。返回 True 表示已进入正文。"""
        if not await self._has_password_box(page):
            return True
        if not (pwd or "").strip():
            # 没密码可填就别去点提交 —— 提交个空值纯属浪费一次往返，
            # 还可能触发页面自己的错误提示，干扰后面的判定。
            return False
        self.log.log("检测到访问密码输入框，自动填写中…")
        try:
            await page.fill("input[type='password']", pwd)
            await page.click(
                "button:has-text('确认'), button:has-text('进入'), "
                "button:has-text('确定'), button:has-text('打开')"
            )
        except Exception:
            pass
        await asyncio.sleep(2.5)
        return not await self._has_password_box(page)

    async def resolve_password(self, page, url, timeout=300.0):
        """等密码：终端输入 或 主人在浏览器里自己输 —— **谁先完成算谁的**。

        返回 ("console", 明文) / ("browser", "") / ("timeout", "")。

        这是本文件里唯一需要跟人抢输入的地方。以前卡在 input() 上死等，
        主人在浏览器里输完了程序也不知道；现在两边同时盯，浏览器里一进正文
        就立刻放行。
        """
        reader = stdinio.get_reader()
        if not reader.available:
            self.log.log("  当前没有终端输入可用，请在浏览器窗口里直接输入密码。")
            if await self._wait_box_gone(page, timeout):
                return "browser", ""
            return "timeout", ""

        print(
            "  请输入这次的文档访问密码（也可以直接在浏览器窗口里输入，两种都行）：",
            end="",
            flush=True,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # 先看浏览器：可能主人已经自己输进去了
            if not await self._has_password_box(page):
                print()
                return "browser", ""
            try:
                line = await reader.readline(timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if line is None:  # stdin 关了，只能指望浏览器那边
                if await self._wait_box_gone(page, timeout):
                    return "browser", ""
                return "timeout", ""
            pwd = (line or "").strip()
            if pwd:
                return "console", pwd
            print("  （终端这边先跳过，仍在等你操作；直接在浏览器里输入也可以）")
        print()
        return "timeout", ""

    async def open_doc(self, page, url):
        """打开文档并处理访问密码，返回是否进入正文。"""
        self.log.log(f"打开文档：{url}")
        try:
            await page.goto(
                url, wait_until="domcontentloaded", timeout=self.cfg["nav_timeout_ms"]
            )
        except Exception as e:
            raise RuntimeError(f"页面打开失败：{e}") from e
        await asyncio.sleep(2.0)

        if not await self._has_password_box(page):
            return True  # 没设密码，或已经放行

        pwd = str(self.cfg.get("password") or "").strip()
        if pwd:
            if await self.fill_password(page, pwd):
                self.log.log("已用 config.json 里保存的密码进入。")
                return True
            self.log.log("config.json 里存的密码不对，需要更新。")
        else:
            self.log.log("这个文档设了访问密码。")

        kind, text = await self.resolve_password(page, url)
        if kind == "browser":
            self.log.log("检测到你已在浏览器里输入密码并进入正文，继续。")
            return True
        if kind != "console" or not text:
            self.log.log(f"没拿到密码，跳过：{url}")
            return False

        self.cfg["password"] = text
        try:
            config_mod.save(self.cfg_path, self.cfg)
            self.log.log("密码已写回 config.json，以后会自动填。")
        except Exception as e:
            self.log.log(f"写回 config.json 失败：{e}")

        if await self.fill_password(page, text):
            return True
        self.log.log(f"终端输入的密码不正确，跳过：{url}")
        return False

    # ---------------- 诊断 ----------------

    async def print_diag(self, page, api_hits=None):
        """登录或接口拿不到东西时，输出页面结构便于定位。"""
        self.log.log(f"页面 URL：{page.url}")
        try:
            self.log.log(f"页面标题：{await page.title()}")
        except Exception:
            pass
        self.log.log("---- 诊断：与 wiki/文档树相关的请求 ----")
        if api_hits:
            for u in api_hits[:40]:
                self.log.log(f"  {u}")
        else:
            self.log.log("  (未捕获到 wiki/node 相关请求)")

        self.log.log("---- 诊断：所有 frame ----")
        for i, fr in enumerate(page.frames):
            self.log.log(f"  frame[{i}] url={fr.url[:120]}")

        self.log.log("---- 诊断：页面按钮（前 30） ----")
        for b in await self.eval_js(
            page,
            "() => Array.from(document.querySelectorAll('button')).slice(0,30)"
            ".map(b => ({t:(b.innerText||'').trim().slice(0,12), "
            "a:(b.getAttribute('aria-label')||'').slice(0,18)}))",
            default=[],
        ) or []:
            if b.get("t") or b.get("a"):
                self.log.log(f"  button text='{b.get('t')}' aria='{b.get('a')}'")
