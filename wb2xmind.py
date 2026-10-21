#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把飞书画板的原始节点 JSON 转成 XMind 文件（离线命令行工具）。

用法:
    uv run python wb2xmind.py <input.json> <output.xmind> [sheet 标题]

输入支持两种格式：
  1. 网页内部接口 /space/api/whiteboard/block 的响应（带不带外层 data 都行）；
  2. lark-cli whiteboard +query --output_as raw 导出的原生节点数组。

流水线里这份转换是**自动**的（Markdown 空壳文档会先试 XMind，见
flybook/exporter.py）；这个脚本是给「手上已经有一份 JSON、想单独转一下」
的场合准备的，不联网、不依赖 lark-cli。

⚠️ 原脚本用 lark-cli 下载画板图片再内嵌。这里不调外部命令，所以离线
转换**不会内嵌图片**（节点文字结构完整）。流水线内会自己下载图片。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from flybook import xmind


def main(argv):
    if len(argv) < 3:
        print("用法: uv run python wb2xmind.py <input.json> <output.xmind> [sheet 标题]")
        return 1
    src, dst = Path(argv[1]), Path(argv[2])
    title = argv[3] if len(argv) > 3 else "Sheet 1"

    if not src.exists():
        print(f"错误：找不到输入文件 {src}")
        return 1

    try:
        # utf-8-sig：Windows 上导出的 JSON 常带 BOM，不剥掉 json.loads 会报错
        raw = json.loads(src.read_text(encoding="utf-8-sig"))
    except Exception as e:
        print(f"错误：JSON 解析失败：{e}")
        return 1

    nodes, extras = xmind.parse_nodes(raw)
    if not nodes:
        print(f"错误：{src} 里没有可用的画板节点")
        return 1

    print(f"  节点数：{len(nodes)}")
    print(f"  图片节点数：{sum(len(v) for v in extras.values())}（离线转换不内嵌）")
    try:
        sheet, stats = xmind.make_sheet(nodes, title, extras)
        xmind.write_xmind([sheet], dst)
    except ValueError as e:
        print(f"错误：{e}")
        return 1
    except Exception as e:
        print(f"错误：写 XMind 失败：{e}")
        return 1

    roots = stats["roots"]
    extra = f"，{roots} 个根节点已合并" if roots > 1 else ""
    print(f"  已生成：{dst}（{stats['nodes']} 个节点{extra}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
