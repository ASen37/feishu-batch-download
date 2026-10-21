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
| `flybook/exporter.py` | 单文档导出、产物校验、重试、zip 解压、思维导图转 XMind / 图片 |
| `flybook/xmind.py` | 画板节点树 → XMind（兼容内部接口与 lark-cli 两种格式） |
| `wb2xmind.py` | 离线把一份画板 raw JSON 转成 XMind 的命令行入口 |
| `flybook/throttle.py` | 全局请求节流（429 全体退让，成功若干次收回） |
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
- **知识库树里不只有文档**：上传的附件（`obj_type=12`）和电子表格（`obj_type=3`）
  用 `type=docx` 提交导出都回 `[1014] export file token not found` —— 和「拿
  wiki_token 当 obj_token」是同一个码，别误判成凭证问题。附件没有导出接口，
  走云盘直链 `.../box/stream/download/all/<obj_token>/`；表格只认
  `src_type=sheet` + `xlsx`（`pdf`/`csv`/`md` 回 `[1018] file extension mismatch`）。
  `Exporter._export_nondoc` 靠 1014 触发补查，**不要改成每篇导出前都 get_node**
  —— 那样等于白烧一整轮请求。
- **附件/表格记录（`kind` 为 `file`/`sheet`）不适用 Markdown 的 wrap 规则，也不参与
  `opts` 签名比对**：它们落的是原文件和固定 `.xlsx`，没有 `图片和附件/` 那一层。
  照搬 wrap 会让 `is_current` 每次判「位置不对」，切格式时还会把它们全部重下一遍。
- **`passback` 是 JSON 字符串**，且**仅在 Markdown 带附件时发送**（「仅文本」是
  不发这个键，不是发 `false`）。这些字段是抓包实测的，`export/create` 对任何字段名
  都回 `code=0`，改完没法靠返回码验证。
- **Markdown 带附件时产物可能是 zip**（正文 + 固定的 `图片和附件/`）。同一档选项下
  形态还会变（没附件的文档给的是普通 `.md`），所以按**文件头 `PK`** 判断要不要解压，
  不按选项猜。
- **导出选项变了要让增量跳过失效**：`manifest.opts_sig` 的签名记进清单，
  加/改导出选项时必须同步它，否则老文件会被静默跳过。
- **Markdown 导不出「整篇是一张思维导图/画板」的文档**：只给一个 10 字节的空壳
  （`# 总结\n\n`），且**不报错**。这不是下载失败，重试多少次都一样。
  现在的顺序是：先读画板节点转 XMind（`_xmind_retry`），转不了再导 PDF 抠 JPEG
  （`_mindmap_retry` 后半段）。服务端**没有**导出成图片的接口
  （`png/jpg/svg` 一律 `[1018] file extension mismatch`），界面上那个
  「下载为图片」是前端 canvas 画的，别去找接口。
  判断空壳用 `blank_md`（剥掉 H1 后还剩不剩字），**不要拿体积判** —— 内容本来就少的
  文档（根文档「首页」467 字节）体积也很小。
- **画板 token ≠ 文档 token**。一篇 docx 里可以嵌多个画板，要先用
  `client_vars` 读块结构、取 `type=whiteboard` 的 `data.token`，再拿它问
  `/space/api/whiteboard/block?blockToken=...`。拿 obj_token 直接问画板接口只会
  返回 404 的 HTML 页面（`_request` 报「返回的不是 JSON」），看着像接口变了。
- **画板节点有两种格式，字段完全不一样**：内部接口是 `nodes[].info.textV2.text` /
  `info.mindMap.parentId`；lark-cli `--output_as raw` 是顶层 `text.text` /
  `mind_map.parent_id`，图片节点父指针还在顶层 `parent_id`。`xmind.parse_nodes`
  两种都认，加新字段时两边都要顾。
- **画板图片多数是「形状填充图」，不在顶层 nodes 里**：真正的位置是父节点的
  `children[]` → `info.fillV2.fillStyleList.data[].imageFillItem`，token 在
  `resource.key`（`imageUrl` 里也能抠）。只找 `info.image` 会漏掉绝大多数截图，
  用户看到的就是「导出的导图里图片全没了」。改了解析规则记得把
  `exporter.XMIND_MARK` 加一版，否则清单会按旧版本号跳过、老文件永远不更新。
- **画板里没有 `mindMap` 就不是思维导图**（自由画板、流程图、空目录页都是这样），
  不能硬转成一张空 XMind，要抛给 PDF 兜底。多个根节点则合成一个虚拟根，
  **别学原脚本只留最后一个根** —— 那样会静默丢内容。
- **`manifest` 里 `kind=mindmap` 的老记录要能重导一次**：上一版只有「PDF 抠图」，
  文件在、大小对，`is_done` 会一路放行，新加的 XMind 就永远不生效。
  `_skip_ok` 里用「有 `xmind` 开关 + kind=mindmap + 没有 `xmind` 标记」把它们捞回来；
  本轮查过确实没画板的会打 `xmind="none"`，下轮不再白查。临时失败**不打**
  `none`，否则一次网络抖动会被永久记成结论。
- **PDF 里那张导图会在每一页各挂一次同一个 XObject**，抠图必须按内容去重。
  抠到的是 DCTDecode 流，**原样写 `.jpg`，不要重编码**。
- **绝不判「这篇是空文档」**：同一篇画板文档连导三次 PDF，内容流实测
  860 / 5900 / 16322 字节 —— **服务端渲染时好时坏**，860 那版只有白底加标题，
  跟空文档长得一模一样。早先据此把「接口文档」判空放行，实际丢了一张图。
  现在的规则是「没有位图就多导几轮（`_PDF_ROUNDS`）留内容流最大的那版，
  并且一律把 PDF 留下」；矢量画的画板走的也是这条路（一个图片对象都没有）。
- **`_mindmap_retry` 里的清理不能写成前一个 try 的 `finally`**：`finally` 是在
  代码**继续往下走之前**执行的，会把后面还要用的临时 PDF 删掉
  （实测报「系统找不到指定的文件」）。用第二个 try/finally 包住后半段。
- **HTTP 429 是累计配额打满，不是每秒速率**：单独连打 25 次 `export/create`
  （2 次/秒）没事，一次运行堆 58 个导出任务就会在后半段整片被拦。
  光让每个 worker 各自重试没用（4 个一起 2s/4s 敲，6 秒内全军覆没），
  必须**全体一起停** —— 这就是 `flybook/throttle.py` 存在的理由。
  改并发或重试策略时别忘了它。
- **节流点在 `api._call` / `api.download` 这两个 async 层**，不在 `_request`：
  `_request` 跑在 `to_thread` 的工作线程里，那儿没有事件循环，`await` 不了。
- **`manifest` 里那批「空壳 md 被记成 ok」的老记录**：文件在、大小也对，
  光看 `is_done` 会一路放行，于是修好了也永远不重导。`Exporter._skip_ok` 里
  用「字数 < `SUSPECT_MD_CHARS` 且没打 `kind` 标记」把它们捞回来重查一次。
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
