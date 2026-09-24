#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量下载飞书文档及其子文档为 PDF（纯 HTTP 异步版）。

用法:
  uv run python main.py                  # 使用 config.json 中的 root_url
  uv run python main.py <新链接>          # 更换下载链接(会同步更新 config.json)
  uv run python main.py --relogin         # 强制重新登录(丢弃缓存的凭证)
  uv run python main.py --headless        # 无头模式(需已登录过一次)
  uv run python main.py --diag            # 只查文档树并打印节点表,不下载
  uv run python main.py --force           # 忽略增量清单,强制重新导出全部
  uv run python main.py --format md       # 导出格式:pdf / word / md
  uv run python main.py --comments        # Word/PDF 带上评论(默认不带)
  uv run python main.py --no-attachments  # Markdown 仅文本(默认带附件的全部内容)
  uv run python main.py --out D:\\飞书文档  # 换个保存目录

  上面这些选项给过一次就写回 config.json,下次不带参数直接跑即可沿用。

流程:
  浏览器只出场一次 —— 登录、打开根文档、把 cookie 交出来、退场。
  之后全是纯 HTTP：递归查文档树拿全部 obj_token（每一层都要单独问一次）→
  并发提交导出任务 → 轮询 → 下载 → 校验。不渲染任何页面，不点任何菜单。
  凭证缓存在 .feishu_session.json，失效时自动重开浏览器刷新。
  刷新出来的凭证会**当场验收**（真调一次文档树接口）：列不出整棵树的身份
  （比如只在浏览器里输过访问密码的匿名读者）一律当失败处理，绝不将就 ——
  否则会静默地只导出一部分，看着还挺成功。

  产物按飞书的目录结构分文件夹存放，文件夹名取自父文档标题（见 manifest.safe_name）。

日志: 终端 + logs/run-<时间戳>.log(归档) + run.log(最新一次)。
进度: manifest.json 记录每篇的导出结果,中断后重跑会自动跳过已完成的。
"""

import argparse
import asyncio
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

from flybook import config as config_mod
from flybook import session as session_mod
from flybook.api import ApiError, FeishuApi
from flybook.browser import BrowserSession
from flybook.collector import LinkCollector, build_paths, load_links_file, probe_via_api
from flybook.exporter import Exporter
from flybook.logger import Logger
from flybook.manifest import Manifest, safe_name

# Windows 终端编码容错:特殊字符无法编码时用 ? 代替,避免崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LINKS_PATH = BASE_DIR / "links.txt"
MANIFEST_PATH = BASE_DIR / "manifest.json"
PROFILE_DIR = BASE_DIR / ".browser_profile"
SESSION_PATH = BASE_DIR / session_mod.SESSION_FILE

MARKS = {"ok": "✓", "skip": "→", "fail": "✗"}


def token_of(url):
    """从文档链接里取出 node token(最后一段路径)。"""
    return url.rstrip("/").rsplit("/", 1)[-1]


def fmt_label(cfg):
    """给日志用的一句话格式说明，例如「PDF（不带评论）」。"""
    fmt = config_mod.norm_format(cfg.get("export_format"))
    if fmt == "md":
        return ("Markdown（带附件的全部内容）" if cfg.get("export_attachment")
                else "Markdown（仅文本，不含图片和附件）")
    name = "PDF" if fmt == "pdf" else "Word"
    return f"{name}（{'导出正文及评论' if cfg.get('export_comment') else '仅正文'}）"


async def collect_via_browser(cfg, logger, headless, root_url, host):
    """兜底：开浏览器旁听文档树接口。

    返回 (nodes, root_title)，nodes 是 [(node_token, obj_token, title, parent_token)]。
    只有 API 路径拿不到树时才走这里。旁听是被动接收，不点按钮，
    所以比 UI 操作稳定得多。注意旁听只覆盖页面**请求过**的范围，
    层级未必完整 —— 能走 API 就走 API。
    """
    from playwright.async_api import async_playwright

    logger.log("回退到浏览器旁听文档树…")
    async with async_playwright() as p:
        session = BrowserSession(cfg, logger, PROFILE_DIR, CONFIG_PATH, headless=headless)
        await session.start(p)
        try:
            page = await session.first_page()
            collector = LinkCollector(logger)
            collector.attach(page)  # 必须在第一次导航前挂上
            if not await session.ensure_login(page, root_url):
                return [], ""
            if not await session.open_doc(page, root_url):
                return [], ""
            await collector.wait_for_tree()
            nodes = await collector.collect(page, root_url, host)
            root_tok = root_url.rstrip("/").rsplit("/", 1)[-1]
            return nodes, collector.node_titles.get(root_tok, "")
        finally:
            await session.close()


async def run(cfg, args, logger):
    export_dir = (BASE_DIR / cfg["export_dir"]).resolve()
    export_dir.mkdir(parents=True, exist_ok=True)
    logger.log(f"导出目录: {export_dir}")

    manifest = Manifest(MANIFEST_PATH, export_dir)
    u = urlparse(cfg["root_url"])
    host = f"{u.scheme}://{u.netloc}"
    root_token = token_of(cfg["root_url"])

    stats = {"ok": 0, "skip": 0, "fail": 0}
    failed = []

    def make_api(data):
        return FeishuApi(
            host, data["cookie"], data.get("csrf", ""),
            referer=cfg["root_url"], timeout=cfg["api_timeout"],
        )

    async def refresh_api():
        """开浏览器登录一次，取回新凭证并落盘。"""
        data = await session_mod.refresh(
            cfg, logger, PROFILE_DIR, CONFIG_PATH, cfg["root_url"],
            headless=args.headless,
        )
        if not data:
            return None
        session_mod.save(SESSION_PATH, data["cookie"], data["csrf"], cfg["root_url"])
        logger.log(f"凭证已缓存到 {SESSION_PATH.name}。")
        return make_api(data)

    # ---------- 1. 拿会话 ----------
    api = None
    if not args.relogin:
        cached = session_mod.load(SESSION_PATH)
        if cached:
            logger.log(f"使用缓存的登录凭证（{cached.get('saved_at')}）。")
            api = make_api(cached)
    if api is None:
        api = await refresh_api()
        if api is None:
            logger.log("拿不到登录凭证，退出。")
            return 1

    # ---------- 2. 递归查文档树（凭证失效就重登一次再试） ----------
    # api_failed 只在「接口这条路整个走不通」时为真 —— 空的文档树不算，
    # 根文档底下本来就没子文档是正常情况，别白白去开一次浏览器。
    api_failed = False
    root_obj, nodes, root_title = "", [], ""
    for attempt in (1, 2):
        try:
            root_obj, nodes, root_title = await probe_via_api(api, root_token, logger)
            break
        except ApiError as e:
            logger.log(f"接口调用失败：{e}")
            if attempt == 2:
                api_failed = True
                logger.log("重登之后仍然失败 —— 接口这条路走不通了。")
                break
            logger.log("凭证可能已失效，重新登录…")
            session_mod.clear(SESSION_PATH)
            api = await refresh_api()
            if api is None:
                return 1

    if args.diag:
        logger.log("---- 诊断模式 ----")
        logger.log(f"根文档 obj_token={root_obj or '(空)'}")
        logger.log(f"文档树共 {len(nodes)} 篇（已递归到全部层级），按存放位置列出：")
        dpaths = build_paths(root_token, nodes)
        # 扩展名跟着实际格式走（以前写死 .pdf，导 Markdown 时会打出骗人的路径）
        fmt_ext = config_mod.ext_of(cfg)
        # Markdown 带附件那档每篇还要多折一层自己的同名文件夹（见 exporter.wrap）
        wrap = fmt_ext == "md" and bool(cfg.get("export_attachment"))
        for tok, obj, title, _ptok in nodes[:200]:
            chain = dpaths.get(tok) or []
            # 直接给出会落到哪：目录链 + 自己的文件名。有子文档的那几篇会看到
            # 「第10章-…/第10章-….pdf」—— 文件夹名和文件名一样是**对的**，
            # 因为父文档折进了以自己命名的文件夹（见 collector.build_paths）。
            name = safe_name(title or "", cfg["max_filename_len"])
            parts = [*chain, name, f"{name}.{fmt_ext}"] if wrap else [*chain, f"{name}.{fmt_ext}"]
            logger.log(
                f"  {'/'.join(parts)} | node={tok} | obj={obj or '-'}"
            )
        if len(nodes) > 200:
            logger.log(f"  …还有 {len(nodes) - 200} 篇未列出")
        return 0

    # ---------- 3. 组装待导出清单 ----------
    if not nodes and api_failed:
        # 走到这里说明接口整个用不了了。凭证本身是验收过的（session.refresh
        # 里验过 get_tree），所以更可能是接口改了。退回旁听至少还能捞到一层，
        # 但那**远远不够** —— 必须把话说死，不能让人以为导全了。
        logger.log("")
        logger.log("⚠️  警告：接口路径不可用，正在退回浏览器旁听兜底。")
        logger.log("⚠️  旁听只覆盖页面请求过的范围，**通常只拿得到第一层**，")
        logger.log("⚠️  导出的篇数会明显少于实际。请用 --diag 核对之后再决定是否采用。")
        logger.log("")
        nodes, root_title = await collect_via_browser(
            cfg, logger, args.headless, cfg["root_url"], host
        )

    titles = {tok: title for tok, _, title, _ in nodes if title}
    if root_title:
        titles[root_token] = root_title
    # 祖先标题链，决定每篇落在 downloads/ 下的哪一层
    paths = build_paths(root_token, nodes)
    docs, seen = [], set()

    if root_token and root_token not in seen:
        seen.add(root_token)
        docs.append((root_token, root_obj, titles.get(root_token, ""), []))
    for tok, obj, title, _ptok in nodes:
        if tok and tok not in seen and tok != root_token:
            seen.add(tok)
            docs.append((tok, obj, title or titles.get(tok, ""), paths.get(tok, [])))
    for url in load_links_file(LINKS_PATH):
        tok = token_of(url)
        if tok and tok not in seen:
            seen.add(tok)
            docs.append((tok, "", titles.get(tok, ""), []))
            logger.log(f"  + links.txt 补充：{tok}")

    if not docs:
        logger.log("没找到任何文档，退出。")
        return 1
    logger.log(f"共找到 {len(docs)} 个文档（含根文档），开始导出…")

    # ---------- 4. 并发导出 ----------
    exporter = Exporter(api, manifest, cfg, logger, titles)
    sem = asyncio.Semaphore(max(1, cfg["concurrency"]))
    lock = asyncio.Lock()
    total = len(docs)
    counter = {"done": 0}

    async def worker(tok, obj, title, parents):
        try:
            async with sem:
                status, detail = await exporter.export(tok, obj, title, parents)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            status, detail = "fail", str(e)

        async with lock:
            if status == "fail":
                manifest.record_fail(tok, title, detail)
                manifest.save()
            counter["done"] += 1
            n = counter["done"]
            stats[status] = stats.get(status, 0) + 1
            logger.log(f"[{n}/{total}] {MARKS[status]} {detail}  ({tok})")
            if status == "fail":
                failed.append((tok, detail))
        return status

    tasks = [asyncio.create_task(worker(t, o, ti, pa)) for t, o, ti, pa in docs]
    try:
        await asyncio.gather(*tasks)
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.log("收到中断,正在保存进度…")
        for t in tasks:
            t.cancel()
        raise
    finally:
        manifest.save()

    logger.log("===== 汇总 =====")
    logger.log(f"成功 {stats['ok']} 个,跳过 {stats['skip']} 个,失败 {stats['fail']} 个。")
    for tok, detail in failed:
        logger.log(f"  失败: {tok} —— {detail}")
    if failed:
        logger.log("失败项已记入 manifest.json,下次运行会自动重试。")
    return 0 if not failed else 2


def do_reset(logger):
    """清空登录状态：凭证缓存 + 浏览器 profile。

    两个地方分开存，只清一个往往不够 —— 只删凭证的话，程序重新开浏览器
    取回来的还是同一份（浏览器自己还记着登录态）。
    """
    logger.log("正在清空登录状态…")
    session_mod.clear(SESSION_PATH)
    logger.log(f"  已删除 {SESSION_PATH.name}")
    if not PROFILE_DIR.exists():
        logger.log(f"  {PROFILE_DIR.name}/ 本来就不存在，跳过")
        return
    try:
        shutil.rmtree(PROFILE_DIR)
        logger.log(f"  已删除 {PROFILE_DIR.name}/")
    except Exception as e:
        logger.log(f"  ✗ 删除 {PROFILE_DIR.name}/ 失败：{e}")
        logger.log("    多半是浏览器还开着，关掉窗口再试一次。")


def main():
    parser = argparse.ArgumentParser(description="批量下载飞书文档及子文档为 PDF")
    parser.add_argument("url", nargs="?", help="要下载的文档链接(不填则用 config.json)")
    parser.add_argument("--headless", action="store_true", help="无头模式(需已登录过一次)")
    parser.add_argument("--relogin", action="store_true", help="丢弃缓存凭证,强制重新登录")
    parser.add_argument("--reset", action="store_true",
                        help="清空全部登录状态(凭证+浏览器profile),彻底重来")
    parser.add_argument("--diag", action="store_true", help="只查文档树并打印节点表,不下载")
    parser.add_argument("--force", action="store_true", help="忽略增量清单,强制重新导出全部")
    parser.add_argument("--format", metavar="FMT",
                        choices=("pdf", "word", "md", "docx", "markdown"),
                        help="导出格式:pdf / word / md(不填用 config.json 里的)")
    parser.add_argument("--comments", dest="comments", action="store_true",
                        default=None, help="Word/PDF:导出正文及评论(默认仅正文)")
    parser.add_argument("--no-comments", dest="comments", action="store_false",
                        help="Word/PDF:仅正文(默认)")
    parser.add_argument("--attachments", dest="attachments", action="store_true",
                        default=None, help="Markdown:带附件的全部内容(默认)")
    parser.add_argument("--no-attachments", dest="attachments", action="store_false",
                        help="Markdown:仅文本,不含图片和附件")
    parser.add_argument("--out", metavar="DIR", help="本地保存目录(默认 downloads)")
    args = parser.parse_args()

    try:
        cfg = config_mod.load(CONFIG_PATH)
    except ValueError as e:
        print(e)
        sys.exit(1)

    # 命令行给过的选项一律写回 config.json —— 下次啥都不带直接跑就能沿用
    changed = False
    for key, val in (
        ("root_url", args.url),
        ("export_dir", args.out),
        ("export_comment", args.comments),
        ("export_attachment", args.attachments),
    ):
        if val is not None:
            cfg[key] = val
            changed = True
    if args.format:
        cfg["export_format"] = config_mod.norm_format(args.format)
        changed = True
    if changed:
        config_mod.save(CONFIG_PATH, cfg)

    if not cfg["root_url"]:
        print("请先在 config.json 中填写 root_url,或运行: python main.py <文档链接>")
        sys.exit(1)
    if args.force:
        cfg["skip_existing"] = False

    logger = Logger(BASE_DIR)
    logger.log("===== 开始运行 =====")
    logger.log(
        f"并发 {cfg['concurrency']},重试 {cfg['max_retry']} 次,"
        f"跳过已导出 {'开' if cfg['skip_existing'] else '关'}"
    )
    logger.log(f"导出格式: {fmt_label(cfg)}")
    logger.log(f"本次日志: {logger.archive_path}")

    if args.reset:
        do_reset(logger)
        logger.log("接下来会弹出浏览器，请重新登录。")

    code = 0
    try:
        code = asyncio.run(run(cfg, args, logger))
    except KeyboardInterrupt:
        logger.log("已中断。")
        code = 130
    finally:
        logger.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
