#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公平对比两份 PDF 的全文页数/字数。

用途：inspect_pdf 是「凑够 min_chars 就 break」的校验器，不是统计器。
拿它的 chars 去跟别的数字比会得出错误结论（实测把 5 页完整文档看成了 287 字）。
这个脚本老老实实把每一页都抽完。

用法: uv run python probe_cmp.py 文件A.pdf 文件B.pdf ...
"""

import sys
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:
    sys.exit("缺少 pypdf，请先运行 uv sync")


def stat(path):
    p = Path(path)
    if not p.exists():
        return f"{p.name}: 不存在"
    try:
        reader = PdfReader(str(p))
    except Exception as e:
        return f"{p.name}: 解析失败 {e}"
    chars = 0
    empty = 0
    for pg in reader.pages:
        try:
            n = len((pg.extract_text() or "").strip())
        except Exception:
            n = 0
        if n == 0:
            empty += 1
        chars += n
    tail = f"，其中 {empty} 页抽不出文字" if empty else ""
    return (f"{p.name}: {len(reader.pages)} 页 / {chars} 字 / "
            f"{p.stat().st_size} 字节{tail}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for arg in sys.argv[1:]:
        print(stat(arg))
