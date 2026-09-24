#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动入口：问链接 → 调 main.py。

为什么要有这个文件：.bat 里写中文会踩编码坑 —— cmd 是按**字节偏移**读批处理
文件的，中途 chcp 切代码页会让后面几行的解析错位，中文被切碎成乱码命令，
报出 '请先安装' is not recognized 这种莫名其妙的错。

所以分工是：.bat 保持**纯 ASCII**，只负责切代码页和拉起本文件；
所有中文界面都放在这里，Python 输出 UTF-8，配 chcp 65001 显示完全正常。

用法:
  start.bat                 双击，或命令行直接跑
  start.bat <文档链接>       直接指定链接，跳过询问
  start.bat --force         参数原样透传给 main.py
"""

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from flybook import config as C  # noqa: E402
from flybook import stdinio  # noqa: E402  （要先插好 sys.path）

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CONFIG_PATH = BASE / "config.json"
SESSION_PATH = BASE / ".feishu_session.json"


def print_tips():
    """登录与密码的说明。

    这几件事最容易被误解，所以摆在最前面：
    - 程序用的是独立 profile，「我日常浏览器登录过」不算数；
    - 必须真的登录：只输访问密码只能看单篇，列不出整棵文档树；
    - 密码不是手输在浏览器里，而是在本终端问一次、之后自动记住。
    """
    print("  ── 开始前请留意 ──")
    if SESSION_PATH.exists():
        print("  1) 登录状态已保存。若这次仍弹出了浏览器窗口，说明登录态没了，")
        print("     请在里面**真的扫码登录**飞书（只输访问密码是不够的）。")
    else:
        print("  1) 需要登录飞书：会弹出一个浏览器窗口，请在里扫码或手机号登录。")
        print("     注意：它跟你平时用的浏览器不是同一套登录状态 ——")
        print("     即使你日常浏览器已登录，这里也要再登一次。只需登这一次。")
        print("     ⚠️ 只输文档的访问密码是不够的：密码只能看单篇，")
        print("        列整棵文档树必须登录，否则只会导出一部分。")
    print("  2) 文档若设了访问密码：可以在**本窗口**里输，")
    print("     也可以直接在浏览器窗口里输 —— 两种都行，谁先完成算谁的。")
    print("     输过一次就自动记住，以后不用再输。")
    print("  3) 接下来会问导出格式（PDF / Word / Markdown）和保存位置，")
    print("     直接回车就是沿用上次的选择。")
    print()


def last_url():
    """读上次用过的链接，用于「直接回车继续」。"""
    if not CONFIG_PATH.exists():
        return ""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return str(data.get("root_url") or "")
    except Exception:
        return ""


def ask_url():
    """问链接。返回 链接字符串 / ""（沿用上次）/ None（用户退出）。"""
    last = last_url()
    print()
    print("=" * 52)
    print("            飞书文档批量下载 PDF")
    print("=" * 52)
    print()
    print_tips()
    if last:
        print(f"  上次的链接：{last}")
        print("  直接回车 = 继续用这个链接")
    else:
        print("  这是第一次运行，需要给它一个文档链接。")
    print()
    print("  请输入要下载的飞书文档链接（以 https:// 开头）")
    print("  输入 q 退出")
    print()

    while True:
        try:
            text = stdinio.ask_sync("  链接: ").strip()
        except KeyboardInterrupt:
            print()
            return None
        if text.lower() in ("q", "quit", "exit"):
            return None
        if not text:
            if last:
                return ""
            print("  ! 还没有可用的链接，请粘贴一个飞书文档链接。")
            continue
        if not text.startswith("http"):
            print("  ! 这看起来不是链接 —— 应该以 https:// 开头。")
            continue
        return text


def read_cfg():
    """读 config.json 当前值，用作「直接回车 = 沿用上次」。"""
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def ask_choice(title, options, default_key):
    """选一项。options 是 [(值, 显示文字)]，直接回车用 default_key。"""
    print(f"  {title}")
    for i, (key, text) in enumerate(options, 1):
        print(f"      {i} = {text}{'（默认）' if key == default_key else ''}")
    while True:
        raw = stdinio.ask_sync("     请选择（直接回车用默认）: ").strip()
        if not raw:
            return default_key
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print("     ! 请输入上面的数字序号，或者直接回车。")


def ask_options(flags):
    """问导出格式 / 该格式的选项 / 保存位置，返回要追加给 main.py 的参数。

    命令行已经明确给过的项就不再问 —— 比如带了 --format，就不该再被追问格式。
    """
    cfg = read_cfg()
    args = []

    if "--format" in flags:
        pass  # 命令行已指定，格式相关的都不问
    else:
        fmt = ask_choice(
            "导出格式",
            [("pdf", "PDF"), ("word", "Word"), ("md", "Markdown")],
            C.norm_format(cfg.get("export_format", "pdf")),
        )
        args += ["--format", fmt]
        if fmt == "md":
            # 两种格式的选项是分开的，同一时刻只有一种是有效的
            keep = bool(cfg.get("export_attachment", True))
            picked = ask_choice(
                "Markdown 导出内容",
                [("yes", "带附件的全部内容（图片、附件都在）"),
                 ("no", "仅文本（不含图片和附件）")],
                "yes" if keep else "no",
            )
            args.append("--attachments" if picked == "yes" else "--no-attachments")
        else:
            keep = bool(cfg.get("export_comment", False))
            picked = ask_choice(
                "内容范围",
                [("no", "仅正文"), ("yes", "导出正文及评论")],
                "yes" if keep else "no",
            )
            args.append("--comments" if picked == "yes" else "--no-comments")

    if "--out" not in flags:
        last = str(cfg.get("export_dir") or "downloads")
        print("  保存位置")
        print(f"      直接回车 = {last}")
        print("      相对路径相对于本程序所在目录；也可以填绝对路径，例：D:\\飞书文档")
        raw = stdinio.ask_sync("     目录: ").strip()
        if raw:
            args += ["--out", raw]

    print()
    return args


def wait_exit():
    """双击运行时窗口别一闪而过。

    走 stdinio 而不是 input()：程序里已经有一个后台线程在读 stdin 了，
    直接 input() 会跟它抢同一份输入（症状是某个提示莫名其妙被跳过）。
    """
    try:
        stdinio.ask_sync("\n按回车键关闭窗口…")
    except KeyboardInterrupt:
        pass


def main():
    args = sys.argv[1:]
    flags = [a for a in args if a.startswith("-")]
    given = [a for a in args if not a.startswith("-")]

    extra = []          # 交互里问出来的格式/选项/目录，转成 main.py 的参数
    if given:
        url = given[0]
    elif any(f in ("-h", "--help") for f in flags):
        url = ""          # 只是想看帮助，别拿链接问题打扰人
    else:
        url = ask_url()
        if url is None:
            print("\n  已取消。")
            wait_exit()
            return 0
        extra = ask_options(flags)

    sys.argv = ["main.py"] + ([url] if url else []) + flags + extra
    print()
    from main import main as run_main

    code = 0
    try:
        run_main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 0
    wait_exit()
    return code


if __name__ == "__main__":
    sys.exit(main() or 0)
