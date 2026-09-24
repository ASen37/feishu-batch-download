"""配置：默认值、加载、类型校正与保存。

老版本 config.json 里已有的键继续生效，缺失的键用默认值补齐，
不认识的键（例如已废弃的 sidebar_xpath、渲染相关的 render_*）原样保留、
不参与逻辑 —— 所以旧配置不用手改也能直接跑。
"""

import json
from pathlib import Path

DEFAULTS = {
    "root_url": "",
    "password": "",
    # auto = 自动找能用那个：Chrome → Edge → browser_path → Playwright 自带内核。
    # 也可强制指定：chrome / msedge / custom / chromium
    "browser": "auto",
    # 自定义浏览器 exe 的完整路径（Chromium 内核的才认）。
    # browser=custom 时用它；auto 模式下排在 Chrome、Edge 之后当第三候选。
    "browser_path": "",
    "export_dir": "downloads",
    # 纯 HTTP 并发，不受浏览器标签页数量限制，可以比旧版开得大
    "concurrency": 4,
    "max_retry": 3,
    "retry_backoff": 2.0,
    # 导出是异步任务：提交后按这个间隔轮询 job_status
    "poll_interval": 2.0,
    # 单个导出任务的最长等待（服务端自报 job_timeout 是 600 秒）
    "export_timeout": 300,
    # 导出格式：pdf / word / md。写 docx、markdown 也认（见 norm_format）。
    # 注意跟下面的 export_src_type 是两回事：那个是**源文档**类型，
    # 这个是要**转成**什么。
    "export_format": "pdf",
    # 源文档类型标识（新版文档为 docx），跟导出格式无关
    "export_src_type": "docx",
    # 下面两个是「格式自带的选项」，各管各的格式，另一个格式用不上就被忽略
    "export_comment": False,     # Word/PDF：带不带文档评论
    "export_attachment": True,   # Markdown：带附件的全部内容（False=仅正文）
    "skip_existing": True,
    # 正文最少字数，低于此值判为空白页。默认 0（关闭）：
    # 换到官方导出后，服务端渲染的就是完整文档，「只渲染出视口那点内容」这种
    # 残缺失败模式已经不存在了，这道防线只会误伤真正内容少的文档
    # （实测「其他资料」本来就是个 1 页 5 字的空目录页）。
    # 想严格把关就调大它，比如 50。
    "min_pdf_chars": 0,
    "min_pdf_bytes": 3072,
    # Markdown / 文本产物的小体积门槛，**跟 PDF 分开**。
    # Markdown 本来就短：实测知识库根文档「首页」的正文只有 467 字节、
    # 内容完全正常，套 PDF 的 3072 会被当成「下载失败」误杀。
    # 这里只留一个「明显不可能是正文」的下限，防的是下到错误页面的情况。
    "min_text_bytes": 16,
    "max_filename_len": 120,
    "page_timeout_ms": 5000,
    "nav_timeout_ms": 60000,
    "api_timeout": 60,
}

_INT_KEYS = (
    "concurrency",
    "max_retry",
    "export_timeout",
    "min_pdf_chars",
    "min_pdf_bytes",
    "min_text_bytes",
    "max_filename_len",
    "page_timeout_ms",
    "nav_timeout_ms",
    "api_timeout",
)
_FLOAT_KEYS = ("retry_backoff", "poll_interval")
_STR_KEYS = ("export_src_type", "browser", "browser_path", "export_dir")
_BOOL_KEYS = ("skip_existing", "export_comment", "export_attachment")

# 格式名：用户写什么 → 内部统一叫什么。docx / markdown 当别名认，
# 免得有人照着飞书的说法填了却悄悄退回 pdf。
FORMAT_ALIASES = {
    "pdf": "pdf",
    "word": "word", "docx": "word",
    "md": "md", "markdown": "md",
}
# 内部格式名 → 接口的 file_extension。Word 的扩展名是 docx，别混。
EXT_OF = {"pdf": "pdf", "word": "docx", "md": "md"}


def norm_format(value):
    """把格式名归一成 pdf / word / md；不认识的一律当 pdf。"""
    return FORMAT_ALIASES.get(str(value or "").strip().lower(), "pdf")


def ext_of(cfg):
    """当前配置要导出的文件扩展名（pdf / docx / md）。"""
    return EXT_OF[norm_format(cfg.get("export_format"))]


def _to_bool(value, default):
    """字符串形式的布尔也认（\"false\" / \"0\" / \"no\" / \"off\" 为假）。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    raw = str(value).strip().lower()
    if raw in ("", "none"):
        return default
    return raw not in ("false", "0", "no", "off")


def load(path):
    """读取配置；文件不存在时返回全套默认值。"""
    cfg = dict(DEFAULTS)
    p = Path(path)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            raise ValueError(f"config.json 解析失败：{e}") from e
        if isinstance(data, dict):
            cfg.update(data)
    return coerce(cfg)


def save(path, cfg):
    """写回配置（保持中文与缩进，方便手动编辑）。"""
    Path(path).write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def coerce(cfg):
    """把配置值转成正确类型，坏值回落到默认值，避免运行时崩。"""
    for k in _INT_KEYS:
        try:
            cfg[k] = int(cfg.get(k, DEFAULTS[k]))
        except (TypeError, ValueError):
            cfg[k] = DEFAULTS[k]
    for k in _FLOAT_KEYS:
        try:
            cfg[k] = float(cfg.get(k, DEFAULTS[k]))
        except (TypeError, ValueError):
            cfg[k] = DEFAULTS[k]
    for k in _STR_KEYS:
        v = cfg.get(k, DEFAULTS[k])
        cfg[k] = str(v).strip() if v is not None else DEFAULTS[k]
    for k in _BOOL_KEYS:
        cfg[k] = _to_bool(cfg.get(k), DEFAULTS[k])
    # 归一化放在最后：上面那些键的类型修正都不依赖它，但下游全都依赖它
    cfg["export_format"] = norm_format(cfg.get("export_format"))
    return cfg
