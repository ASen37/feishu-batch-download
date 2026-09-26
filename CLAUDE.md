# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目

把飞书知识库的文档树**递归**批量导出为 PDF / Word / Markdown，按飞书里的目录结构分文件夹存放。

面向使用者的部分（参数表、配置项、产物形态、排查清单）全部在 `README.md`，
写得很细，**改行为之前先读它**。本文只写「改代码需要知道的事」。

代码、注释、日志一律用中文。注释的风格是解释**为什么这么写**、**踩过什么坑** ——
延续这个风格，改代码时把新的坑记在原地。

## 常用命令

```bash
uv sync                              # 装依赖（playwright + pypdf）
uv run python selftest.py            # 离线自检，改完必跑
uv run python main.py                # 按 config.json 跑
uv run python main.py <链接>          # 换链接（会写回 config.json）
uv run python main.py --diag         # 只列文档树 + 每篇的落点，不下载
uv run python probe_cmp.py a.pdf     # 统计 PDF 页数/全文字数
uv run python probe_export.py --ui   # 飞书改接口后重新摸导出参数
start.bat                            # 交互式启动（双击用）
```

`selftest.py` 是**唯一**的测试：顺序脚本，按 `== N. 标题 ==` 分节，没有筛选机制，
要单跑某节就临时注释掉其余部分。不联网、不开浏览器，几秒出结果。

## 流水线

浏览器**只出场一次**（登录 + 取 cookie），之后全是纯 HTTP，不渲染任何页面：

```
session.refresh ── 开浏览器登录、取 cookie/_csrf_token、当场验收（真调一次 get_tree）
   │  凭证缓存到 .feishu_session.json
   ▼
collector.probe_via_api ── 从 root_token 起 BFS，逐层展开整棵文档树
   ▼
main.run ── 组装 docs（根文档 + 树 + links.txt 补充），Semaphore 并发
   ▼
Exporter.export ── 提交导出任务 → 轮询 → 下到临时文件 → 校验 → 落盘 → 记 manifest
```

| 文件 | 职责 |
| --- | --- |
| `flybook/api.py` | 飞书**内部接口**客户端。所有接口都集中在这里，失效只改这一处 |
| `flybook/session.py` | 凭证的获取、缓存、刷新与**验收** |
| `flybook/browser.py` | 浏览器会话，只用于登录与取凭证 |
| `flybook/collector.py` | 文档树抓取（API 优先）+ 目录链计算 `build_paths` |
| `flybook/exporter.py` | 单文档导出、产物校验、重试、zip 解压 |
| `flybook/manifest.py` | 增量清单、文件名安全化、落点分配 |
| `flybook/config.py` | 默认值、加载、类型校正 |
| `flybook/stdinio.py` | 后台 stdin 读取（终端输入与浏览器操作赛跑） |
| `flybook/logger.py` | 日志：终端 + `logs/run-*.log` 归档 + `run.log` |
| `main.py` / `start.py` | CLI 入口与并发编排 / 交互式启动界面 |

## 改代码前必须知道的坑

这些每一条都是踩出来的，照直觉改会静默出错：

- **`get_tree` 只返回一层**，不是整棵树。必须 BFS 逐层问（`collector.probe_via_api`）。
  早期版本调一次就当全树，41 篇的库只导出了 15 篇**且不报错**。
- **鉴权头是 `x-csrftoken`**（无连字符）。写成 `x-csrf-token` 会收到 403
  "csrf token error"，看着像没权限。
- **`get_tree` 的参数名必须是 `wiki_token`**。传 `space_id`（值对也一样）会得到
  `[920004004] PermFail`，同样看着像没权限。
- **取 node token 一律走 `api.token_of()`**，它剥掉 `?query` 和 `#fragment`。
  飞书「复制链接」给的地址带 `?from=from_copylink`，而早期 5 处各写各的
  `url.rsplit("/", 1)[-1]`，全都只切斜杠、不剥查询串，于是服务端回
  `[920004002] SourceNotExist`，看着像文档不存在。别再自己切字符串。
- **两个错误码含义相反，别混**：`920004004 PermFail` = 身份不够格（该重登）；
  `920004002 SourceNotExist` = 节点不存在（该改链接，重登没用）。
  `session.verify` 对后者不重试，`session._diagnose` 按码给不同的下一步。
- **`passback` 是 JSON 字符串**，且**仅在 Markdown 带附件时发送**（「仅文本」是
  不发这个键，不是发 `false`）。这些字段是抓包实测的，`export/create` 对任何字段名
  都回 `code=0`，改完没法靠返回码验证。
- **Markdown 带附件时产物可能是 zip**（正文 + 固定的 `图片和附件/`）。同一档选项下
  形态还会变（没附件的文档给的是普通 `.md`），所以按**文件头 `PK`** 判断要不要解压，
  不按选项猜。
- **导出选项变了要让增量跳过失效**：`manifest.opts_sig` 的签名记进清单，
  加/改导出选项时必须同步它，否则老文件会被静默跳过。
- **`safe_name` 连点号一起替换**，所以它只处理标题，扩展名由调用方拼上去。
- **所有终端输入走 `stdinio`**。直接 `input()` 会跟后台读取线程抢同一份 stdin
  （症状是某个提示莫名其妙被跳过）。
- **`start.bat` 保持纯 ASCII**：cmd 按字节偏移读批处理文件，中途 `chcp` 会让解析错位。
  中文界面全在 `start.py`。
- **命令行参数会写回 `config.json`** —— 包括 `--diag <链接>` 里的那个链接，
  拿别的链接试诊断会把正在用的换掉。

## 环境与文件

- `config.json`、`.feishu_session.json`、`.browser_profile/`、`downloads/`、`manifest.json`
  都**不进版本库**（见 `.gitignore`）：前三个装着文档链接、访问密码和账号真实登录态。
- `config.json` 常被程序改写（CLI 参数、访问密码），改动它不算改代码。
- 排查看 `logs/run-<时间戳>.log` 或 `run.log`。
- `handover/` 是历史交接文档（已在 .gitignore 里），只在需要追溯早期设计时翻。
