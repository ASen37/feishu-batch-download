"""单文档导出：提交导出任务 → 轮询 → 下载 → 校验，失败按指数退避重试。

这是本项目第二次换导出路径，两次都是因为「假成功」：

- v0.1 假设页面总会加载好，拿到 wait_content_loaded 的返回值却不看，
  于是两篇没加载出来的文档被统计成了成功。
- v0.2 换成了 page.pdf() 打印 + inspect_pdf 校验，把「完全空白」拦住了，
  但没拦住**内容残缺**：打印只能拿到当前视口里已渲染的 DOM，
  同一篇文档打印出来是 1 页 852 字，官方导出是 5 页 3150 字，
  而 852 字照样能过「字数 > 50」的校验，于是又成了假成功。

现在改走官方的异步导出任务，服务端拿数据库里的完整文档渲染，
产物与手工导出逐字一致，不再受视口、懒加载、滚动容器的任何影响。
"""

import asyncio
import hashlib
import json
import logging
import re
import shutil
import stat
import struct
import tempfile
import zipfile
import zlib
from pathlib import Path

from . import xmind
from .api import ERR_EXPORT_TOKEN_NOT_FOUND, ApiError, is_rate_limited
from .config import ext_of
from .manifest import opts_sig

# 日志里说人话用的格式名（`docx` 说成 Word，省得看日志的人愣一下）
EXT_LABEL = {"pdf": "PDF", "docx": "Word", "md": "Markdown"}

# 兜底时最多导几份 PDF，留内容最全的那版。
#
# 第一份就给到位图的话只导一次 —— 思维导图都是这样，占绝大多数。
# 抠不到位图才会多导，因为**同一篇文档服务端的渲染会时好时坏**：实测
# 「接口文档」这篇连着导三次，内容流分别是 860 / 5900 / 16322 字节，
# 只有最后一版是画全了的。860 那版跟「真空文档」长得一模一样 ——
# 所以不能用任何门槛去判「这篇没内容」，只能多导几次挑最大的。
_PDF_ROUNDS = 3

# XMind 产物的规则版本。**改了节点解析方式（比如这次补上画板填充图）就要
# 往上加一版**：清单里记着旧版本号的记录会被判为过期、重导一次，否则文件在、
# 大小对，is_done 会一路放行，用户看到的还是缺图的老文件。
XMIND_MARK = "v2"

# 「字数少得可疑」的 Markdown 门槛，用来捞出清单里那批老的空壳记录。
# 空壳实测是 10~12 字节（`# 总结\n\n`）；飞书给的标题本身就是几个字，
# 所以数的是 md 里的字符数（`# 总结` 算 3 个字），门槛取 32 足够宽松 ——
# 它只影响「要不要重查一次」，不会误判产物好坏，宽一点无所谓。
SUSPECT_MD_CHARS = 32

# 飞书节点的 obj_type。**知识库里不只有文档**：上传的附件和电子表格也在树里，
# 而且它们用 docx 提交导出时一律回 [1014] export file token not found，
# 跟「拿 wiki_token 当 obj_token 用」是同一个码，很容易误判成凭证问题：
#   12 = 用户上传的文件（PDF / MD / DOCX…）：没有导出接口，只能走云盘直链下载；
#    3 = 电子表格：能导出，但**只认 xlsx**（pdf / csv / md 都回
#        [1018] export file extension  mismatch，实测）。
OBJ_TYPE_SHEET = 3
OBJ_TYPE_FILE = 12

# 不走导出、直接落盘/按原格式落盘的 kind（影响跳过判断里 wrap 的取值，见 _skip_ok）
NATIVE_KINDS = ("file", "sheet")


def _file_ext_of(node, title):
    """上传文件该用什么扩展名：优先 icon_info.file_type，再退到标题后缀。

    icon_info 是**字符串形式的 JSON**（接口原样返回），里面 file_type 实测
    就是 `pdf` / `md` 这种不带点的扩展名。
    """
    info = node.get("icon_info")
    if isinstance(info, str):
        try:
            info = json.loads(info)
        except ValueError:
            info = {}
    ext = str((info or {}).get("file_type") or "").strip().lower()
    if not ext:
        ext = Path(title or "").suffix.lstrip(".").lower()
    return ext


def _drop_ext(title, ext):
    """标题本身已带扩展名时先剥掉，别让 allocate_path 再拼一次。

    上传的附件标题就长这样：`整合华为云IOT的方案.md`。不剥的话，
    safe_name 会把点换成下划线，最后落成 `整合华为云IOT的方案_md.md`。
    """
    suffix = f".{ext}" if ext else ""
    if suffix and title.lower().endswith(suffix):
        return title[: -len(suffix)]
    return title


try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None

# pypdf 遇到损坏文件会往 stderr 打警告；我们本来就捕获异常并给出中文原因，
# 让它别重复刷屏。
logging.getLogger("pypdf").setLevel(logging.ERROR)


def count_images(reader, limit=50):
    """数 PDF 里的图片张数（够 limit 张就早退，不数完）。

    图片型文档（比如满屏截图的接口文档）文字天然很少：
    实测「接口文档」30KB、1 页、只有 5 个字，但内容是实打实的。
    只看字数会把它误判成空白页。
    """
    n = 0
    for pg in reader.pages:
        try:
            res = pg.get("/Resources")
            if res is None:
                continue
            if hasattr(res, "get_object"):
                res = res.get_object()
            xobj = res.get("/XObject")
            if not xobj:
                continue
            if hasattr(xobj, "get_object"):
                xobj = xobj.get_object()
            for name in list(xobj.keys()):
                try:
                    if xobj[name].get_object().get("/Subtype") == "/Image":
                        n += 1
                        if n >= limit:
                            return n
                except Exception:
                    continue
        except Exception:
            continue
    return n


def _pdf_colors(obj):
    """这个图片 XObject 有几个颜色分量。认不出来返回 0。"""
    cs = obj.get("/ColorSpace")
    try:
        if hasattr(cs, "get_object"):
            cs = cs.get_object()
    except Exception:
        return 0
    if isinstance(cs, list) and cs:
        head = cs[0]
        # ICCBased 的分量数写在它自己的流字典 /N 上
        if str(head) == "/ICCBased" and len(cs) > 1:
            try:
                prof = cs[1].get_object()
                return int(prof.get("/N") or 0)
            except Exception:
                return 0
        cs = head
    return {"/DeviceGray": 1, "/DeviceRGB": 3, "/DeviceCMYK": 4}.get(str(cs), 0)


def _png_bytes(raw, width, height, colors):
    """把解压好的裸位图拼成 PNG（8 位/通道）。

    只在 FlateDecode 那档用得上 —— 飞书给画板渲染的图基本都是 JPEG，
    但万一换成 PNG 压缩，不认就会把它当成「这篇是空文档」，那是静默丢内容。
    """
    if colors not in (1, 3, 4) or width <= 0 or height <= 0:
        return None
    stride = width * colors
    if len(raw) < stride * height:
        return None
    # PNG 每个扫描行前面要加一个「滤波方式」字节，0 = 不过滤
    scanlines = b"".join(
        b"\x00" + raw[y * stride:(y + 1) * stride] for y in range(height)
    )

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8,
                       {1: 0, 3: 2, 4: 6}[colors], 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(scanlines, 6)) + chunk(b"IEND", b""))


def extract_pdf_images(reader):
    """把 PDF 里嵌的图片原样抠出来，返回 [(扩展名, 字节), ...]（保序、按内容去重）。

    **为什么直接搬字节、不重编码**：飞书给思维导图/画板渲染的是 DCTDecode
    流 —— 就是一张现成的 JPEG（实测 2560×2560）。它本来就是服务端渲染好的
    成品图，再过一道编解码只会掉画质、还慢。所以 DCTDecode 原样写 `.jpg`。

    去重是必须的：同一张导图会在 PDF 的每一页各挂一次（实测 2 页都挂着
    同一个 XObject），不去重就会把同一张图存两遍。

    抠不到返回空表 —— 调用方据此判断「这篇是真的空文档」。
    """
    out, seen = [], set()
    for pg in reader.pages:
        try:
            res = pg.get("/Resources")
            if hasattr(res, "get_object"):
                res = res.get_object()
            xobj = res.get("/XObject") if res else None
            if hasattr(xobj, "get_object"):
                xobj = xobj.get_object()
            items = list(xobj.items()) if xobj else []
        except Exception:
            continue
        for _name, ref in items:
            try:
                obj = ref.get_object()
            except Exception:
                continue
            try:
                if obj.get("/Subtype") != "/Image":
                    continue
            except Exception:
                continue

            filt = obj.get("/Filter")
            if isinstance(filt, list):
                filt = filt[-1] if filt else None
            filt = str(filt or "")
            try:
                if filt == "/DCTDecode":
                    # 私有属性有点丑，但 pypdf 没有公开 API 拿「未解码的流」，
                    # 而 get_data() 对 DCTDecode 的行为不值得赌（有的版本会
                    # 试图解码、有的原样返回）。这里要的就是原始字节。
                    data = bytes(obj._data)
                    ext = "jpg"
                elif filt == "/FlateDecode":
                    data = _png_bytes(
                        obj.get_data(),
                        int(obj.get("/Width") or 0),
                        int(obj.get("/Height") or 0),
                        _pdf_colors(obj),
                    )
                    if data is None:
                        continue
                    ext = "png"
                else:
                    continue
            except Exception:
                continue

            key = hashlib.sha1(data).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            out.append((ext, data))
    return out


def inspect_pdf(path, min_chars=0, min_bytes=3072):
    """检查 PDF 是不是「假成功」。返回 {ok, pages, chars, size, images, reason}。

    chars 是**全文字数**（每一页都抽完），不是抽样值 —— manifest 里记的
    质量指标要能直接跟官方导出对比，数字不准就没意义了。

    min_chars 默认 0（不做字数校验）：走官方导出之后，服务端渲染的就是完整
    文档，不再有「只渲染出视口那点内容」的残缺产物，字数门槛只会误伤真正
    内容少的文档。想严格把关就把它调大。
    """
    info = {"ok": False, "pages": 0, "chars": 0, "size": 0, "images": 0, "reason": ""}
    p = Path(path)
    if not p.exists():
        info["reason"] = "文件不存在"
        return info
    info["size"] = p.stat().st_size
    if info["size"] < min_bytes:
        info["reason"] = f"文件过小（{info['size']} < {min_bytes} 字节）"
        return info
    if PdfReader is None:
        info["reason"] = "缺少 pypdf，请先运行 uv sync"
        return info
    try:
        reader = PdfReader(str(p))
        info["pages"] = len(reader.pages)
    except Exception as e:
        info["reason"] = f"PDF 解析失败：{e}"
        return info
    if info["pages"] < 1:
        info["reason"] = "页数为 0"
        return info

    chars = 0
    for pg in reader.pages:
        try:
            chars += len((pg.extract_text() or "").strip())
        except Exception:
            continue
    info["chars"] = chars
    info["images"] = count_images(reader)

    if min_chars > 0 and chars < min_chars:
        # 图片型文档：以截图为主的说明页（接口文档之类），文字天然就少，
        # 但内容是真的，不能因为它字数少就判成空白。
        if info["images"] > 0:
            info["ok"] = True
            info["reason"] = (
                f"{info['pages']} 页 / {chars} 字 / {info['images']} 张图 / "
                f"{info['size']} 字节（图片型文档，按图放行）"
            )
            return info
        # 个别 PDF 字体没有 ToUnicode 映射会导致抽不出文字。
        # 文件够大且不止一页时降级放行，避免把正常文档误判成空白页。
        if info["size"] >= min_bytes * 8 and info["pages"] >= 2:
            info["ok"] = True
            info["reason"] = (
                f"{info['pages']} 页 / {info['size']} 字节"
                f"（文本抽取为 0，疑似字体无 ToUnicode，判定为有效）"
            )
            return info
        info["reason"] = f"正文文字过少（{chars} < {min_chars} 字）"
        return info

    info["ok"] = True
    info["reason"] = f"{info['pages']} 页 / {chars} 字 / {info['size']} 字节"
    return info


def inspect_output(path, ext, min_chars=0, min_bytes=3072):
    """按格式校验产物，返回和 inspect_pdf 同形状的 dict。

    PDF 走 inspect_pdf（能读出页数字数）；docx 和 md **不能**套那一套 ——
    docx 是个 ZIP 包、md 是纯文本，拿 pypdf 去读只会全部判成「解析失败」，
    41 篇一篇都存不下。

    docx 只验体积 + ZIP 文件头；md 验体积 + 能按 UTF-8 解出非空文本。
    这两种格式没有页数概念，pages 一律记 0。
    """
    if ext == "pdf":
        return inspect_pdf(path, min_chars, min_bytes)

    info = {"ok": False, "pages": 0, "chars": 0, "size": 0, "images": 0, "reason": ""}
    p = Path(path)
    if not p.exists():
        info["reason"] = "文件不存在"
        return info
    info["size"] = p.stat().st_size
    if info["size"] < min_bytes:
        info["reason"] = f"文件过小（{info['size']} < {min_bytes} 字节）"
        return info
    try:
        raw = p.read_bytes()
    except Exception as e:
        info["reason"] = f"读不出内容：{e}"
        return info

    if ext == "docx":
        if raw[:2] != b"PK":
            info["reason"] = "不是有效的 Word 文件（缺少 ZIP 头）"
            return info
        info["ok"] = True
        info["reason"] = f"{info['size']} 字节（Word）"
        return info

    # md：能解出非空文本就算过
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        info["reason"] = "不是 UTF-8 文本（可能下错了格式）"
        return info
    info["chars"] = len(text.strip())
    if not info["chars"]:
        info["reason"] = "内容为空"
        return info
    info["ok"] = True
    info["reason"] = f"{info['chars']} 字（Markdown）"
    return info


_RE_H1 = re.compile(r"^\s*#\s+(.*)$")


def blank_md(path):
    """这份 Markdown 是不是「空壳」？返回 (是否空壳, 从里面读到的标题)。

    什么叫空壳：剥掉开头那行 H1 标题之后，一个字都不剩。

    这个判断专治**思维导图 / 画板文档**。实测这类文档导成 Markdown 时，
    产物是 10 个字节的 `# 总结\\n\\n` —— 正文一个字没有，服务端也不报错。
    早先它被当成「文件过小」判失败，于是每轮重跑都失败一遍，而真相是
    **这个格式导不出它的内容**，重试一万次也一样，得换条路（见 _mindmap_retry）。

    为什么不能只看体积：真正内容少的文档（比如知识库根文档「首页」只有
    467 字节）体积也很小，拿体积当信号会误伤。剥掉标题之后还剩不剩字，
    才分得清「短」和「空」。
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False, ""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    title = ""
    if i < len(lines):
        m = _RE_H1.match(lines[i])
        if m:
            # 飞书把标题里的 `-` 之类转义成了 `\\-`，还原一下再当标题用
            title = m.group(1).strip().replace("\\", "")
            i += 1
    return (not "\n".join(lines[i:]).strip()), title


def pdf_ink_bytes(reader):
    """把所有页面内容流的字节数加起来 —— 「这页到底画了东西没有」的粗略量。

    为什么需要它：判「这篇是不是空文档」不能只看有没有位图。**矢量画的画板**
    在 PDF 里一个图片对象都没有，但内容是真的。实测空的画板文档
    （整页就是一个白色底 + 一行标题）内容流是 860 字节 —— 有指令，但没内容。

    读不出来时返回一个超过门槛的值：**读不出来就当它有东西**。
    这一头错了只是多留个文件，另一头错了是把内容丢掉。
    """
    total = 0
    for pg in reader.pages:
        try:
            c = pg.get_contents()
            if c is not None:
                total += len(c.get_data())
        except Exception:
            return _VECTOR_INK_MIN
    return total


def read_pdf_images(path):
    """读一个 PDF，返回 (图片总数, [(扩展名, 字节), ...], 内容流字节数)。

    **总数和「抠出来的」要分开报**：总数 > 0 却一张都没抠出来，说明那是我们
    不认识的图片流，这时绝不能反过来说「这篇文档是空的」—— 那等于把内容
    丢了还报成功。

    PDF 解析失败返回 (None, [], 0)，跟「真的没有图」(0, [], N) 区分开。
    """
    if PdfReader is None:
        return None, [], 0
    try:
        reader = PdfReader(str(path))
    except Exception:
        return None, [], 0
    try:
        return count_images(reader), extract_pdf_images(reader), pdf_ink_bytes(reader)
    except Exception:
        return None, [], 0


def is_zip(path):
    """文件头是不是 ZIP（`PK`）。

    只用来分辨 Markdown 的两种产物形态：选「带附件的全部内容」时，
    **有附件**的文档飞书给的是 zip，**没有附件**的给的是普通 .md ——
    看选项猜不出来，实测同一次运行里两种都会出现，只能看字节。
    （docx 也是 PK 开头，但这个判断只在 md 分支用，不会串。）
    """
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"PK"
    except OSError:
        return False


def _unsafe_reason(info):
    """这个 zip 条目能不能安全解压？不安全就返回原因。

    两道检查：
    1. **zip slip**（老牌攻击面）：条目名写成 `../../x` 或绝对路径
       （`C:\\x`），解压时就落到目标目录外面去了；
    2. **符号链接**：条目本身是条软链，后面再有一条经过它写文件，照样能写到外面。

    飞书未必会这么干 —— 但解压别人给的包本来就该先验一遍，成本极低。
    """
    name = info.filename.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return f"绝对路径 {name}"
    if ".." in [p for p in name.split("/") if p]:
        return f"含上级跳转 {name}"
    if stat.S_ISLNK(info.external_attr >> 16):
        return f"符号链接 {name}"
    return ""


def extract_zip(src, dst_dir, stem):
    """解开导出得到的 zip，返回正文 .md 的路径（已改名成 stem）。

    飞书「带附件的全部内容」的结构是：正文 .md 在根，附件在 `图片和附件/` 下。

    正文会**重命名成跟所在文件夹同名** —— zip 里那个名字是文档原始标题，
    而落盘用的文件夹名过了 safe_name（点、斜杠之类都换成了下划线），
    两个名字往往对不上，统一成文件夹名才可预测。之后 manifest 记的
    就是这条路径，下次跳过判断才认得出是同一份。
    """
    src, dst_dir = Path(src), Path(dst_dir)
    with zipfile.ZipFile(src) as z:
        for info in z.infolist():
            bad = _unsafe_reason(info)
            if bad:
                raise RuntimeError(f"zip 里有不安全的条目，已拒绝解压（{bad}）")
        z.extractall(dst_dir)

    # 正文本体：优先根目录下的 .md，没有就退而取最大的那个
    mds = sorted(dst_dir.glob("*.md"))
    if not mds:
        mds = sorted(dst_dir.rglob("*.md"), key=lambda p: p.stat().st_size, reverse=True)
    if not mds:
        raise RuntimeError("zip 里没找到 Markdown 正文")
    main = mds[0]

    want = dst_dir / f"{stem}.md"
    if main != want:
        main.replace(want)
    return want


class Exporter:
    def __init__(self, api, manifest, cfg, logger, titles):
        self.api = api
        self.manifest = manifest
        self.cfg = cfg
        self.log = logger
        self.titles = titles
        # 本次运行的导出选项。签名会记进清单：换格式或换选项之后，
        # 老产物明明还在原地，光看 is_done 会被静默跳过。
        self.ext = ext_of(cfg)
        self.comment = bool(cfg.get("export_comment"))
        self.attachment = bool(cfg.get("export_attachment"))
        self.sig = opts_sig(self.ext, self.comment, self.attachment)
        self.ext_label = EXT_LABEL.get(self.ext, self.ext.upper())
        # Markdown 带附件那档，飞书给的是 zip（正文 + `图片和附件/`）。
        # 那个附件目录名是**固定的**，两篇文档落在同一个父目录下就会互相覆盖
        # 图片，所以每篇都得折进自己的同名文件夹（见 manifest.allocate_path）。
        # 注意这里按**选项**决定，不是按字节：落点必须在下载**之前**就定下来
        # （跳过判断也要用），而「这篇到底有没有附件」得下完才知道。
        # 没有附件的文档因此会多包一层只装一个 .md 的文件夹 —— 换来的是
        # 落点可预测、跳过判断可靠，这个交换划算。
        self.wrap = self.ext == "md" and self.attachment
        # 思维导图/画板文档的兜底开关，只对 Markdown 档有意义（见 _mindmap_retry）。
        # xmind 是首选（结构完整、可编辑），image 是它的退路。
        self.xmind = bool(cfg.get("mindmap_to_xmind", True))
        self.mindmap = bool(cfg.get("mindmap_to_image", True))
        # 本次运行走了兜底的篇数，汇总时报给主人。worker 都在同一个事件循环里
        # 跑，自增之间没有 await，不会丢计数。
        self.xminds = 0
        self.mindmaps = 0

    def _skip_ok(self, node_token, title, parents):
        """这篇能不能直接跳过？"""
        rec = self.manifest.get(node_token) or {}
        # 上传的文件和电子表格是原样落盘的，**没有** `图片和附件/` 那一层，
        # 所以 Markdown 带附件时的 wrap 规则对它们不适用。照搬的话
        # is_current 会要求多一层同名文件夹，每次都判「位置不对」→ 永远重下。
        native = rec.get("kind") in NATIVE_KINDS
        wrap = self.wrap and not native
        # 附件原文件和表格导出的 xlsx 都跟本次选的导出格式无关：上传的 PDF
        # 不会因为切到 Word 档就变成另一个文件。所以这两类记录不参与
        # opts 签名比对，否则每换一次格式都要把附件重下一遍。
        sig = (rec.get("opts") or self.sig) if native else self.sig
        if not (self.manifest.is_done(node_token, self.cfg.get("skip_existing", True))
                and self.manifest.is_current(
                    node_token, parents, sig, self.cfg["max_filename_len"],
                    wrap=wrap, title=title)):
            return False
        # 上一版只有「导 PDF 抠图」这条兜底，清单里记的是 kind=mindmap 且没有
        # xmind 标记。现在能转 XMind 了，让它们重导一次 —— 否则文件在、大小对，
        # is_done 会一路放行，新功能对这些文档永远不生效。
        # 本轮如果查过发现确实没有画板，会打上 xmind="none"，下轮就不再重查。
        if self.xmind and rec.get("kind") == "mindmap" and not rec.get("xmind"):
            return False
        # 规则版本旧了也要重导（例如 v1 漏掉了画板里的填充图）。
        if (self.xmind and rec.get("kind") == "xmind"
                and rec.get("xmind") != XMIND_MARK):
            return False
        # Markdown 档还要多看一眼：清单里存着一批**空壳 md 被记成 ok** 的老记录
        # （加导图兜底之前导的，正文只有一行标题）。它们文件在、大小也对，
        # is_done 会一路放行，结果是「修好了也永远不重导」。
        # 只挑「字数少得可疑、又没打 kind 标记」的重查一次，别的 md 记录不受影响。
        if self.ext == "md":
            if not rec.get("kind") and (rec.get("chars") or 0) < SUSPECT_MD_CHARS:
                return False
            # 「空文档」那条结论已经删掉了 —— 它是错的（画板渲染时好时坏，
            # 860 字节那版跟真空文档长得一模一样）。那批记录的字数被写成 0，
            # 正好拿来认：正常的 md 记录字数一定 > 0（空文本压根过不了校验）。
            if rec.get("kind") == "doc" and not rec.get("chars"):
                return False
        return True

    async def export(self, node_token, obj_token, title, parents=None):
        """导出单篇。返回 (status, detail)，status ∈ {ok, skip, fail}。

        parents 是祖先标题链，用来决定这篇落在哪个子目录（见 manifest.allocate_path）。
        """
        # 除了「下过没下过」，还得看「位置和选项对不对」：目录规则变了、或者
        # 换了格式/评论选项时，老文件还在老地方，只比 is_done 会把它们全放过。
        if self._skip_ok(node_token, title, parents):
            rec = self.manifest.get(node_token) or {}
            return "skip", rec.get("file", "")

        if not title:
            title = (self.manifest.get(node_token) or {}).get("title") or node_token

        attempts = max(1, self.cfg["max_retry"])
        backoff = self.cfg["retry_backoff"]
        rate_backoff = float(self.cfg.get("retry_429_backoff", backoff))
        last_err = "未知错误"
        # 限流时临时放宽的「这次最多试几次」。配额窗口以分钟计，
        # 3 次 × 2 秒的老节奏等于没等（2026-10-16 就是这么整片失败的）。
        limit = attempts
        attempt = 0

        while attempt < limit:
            attempt += 1
            try:
                if not obj_token:
                    # 浏览器兜底路径抓到的节点只带 node_token，这里补换一次
                    node = await self.api.get_node(node_token)
                    obj_token = str(node.get("obj_token") or "")
                    if not obj_token:
                        raise RuntimeError("这个节点拿不到 obj_token")
                out, info = await self._once(obj_token, node_token, title, parents)
            except Exception as e:
                last_err = str(e)
                # 1014 不一定是 token 的问题：上传的附件和电子表格用 docx 提交
                # 也是这个码。补查节点类型，能走旁路就走旁路；否则再按原逻辑重试。
                if (isinstance(e, ApiError)
                        and e.code == ERR_EXPORT_TOKEN_NOT_FOUND and obj_token):
                    alt = await self._export_nondoc(node_token, obj_token,
                                                    title, parents)
                    if alt is not None:
                        return alt
                if is_rate_limited(e):
                    # 429 不是这篇的问题，多给几次机会
                    limit = max(limit, attempts + 2)
                    wait = min(60.0, rate_backoff * (2 ** (attempt - 1)))
                else:
                    wait = backoff * (2 ** (attempt - 1))
                self.log.log(f"  第 {attempt}/{limit} 次失败：{last_err}")
                if attempt < limit:
                    self.log.log(f"  等待 {wait:.0f} 秒后重试…")
                    await asyncio.sleep(wait)
                continue

            kind = info.get("kind", "doc")
            if kind == "xmind":
                self.xminds += 1
            elif kind == "mindmap":
                self.mindmaps += 1
            self.manifest.record_ok(node_token, title, out, info["pages"],
                                    info["chars"], self.sig,
                                    kind=kind, images=info.get("images", 0),
                                    xmind=info.get("xmind", ""))
            self.manifest.save()
            rel = self._rel(out)
            # PDF 保留原来的「N 页 / M 字」；docx/md 没有页数概念，直接用它自己的
            # reason（形如「4197 字节（docx）」「467 字（markdown）」）
            quality = (
                f"{info['pages']} 页 / {info['chars']} 字"
                if self.ext == "pdf"
                else info["reason"]
            )
            return "ok", f"{rel}（{quality}）"

        self.manifest.record_fail(node_token, title, last_err)
        self.manifest.save()
        return "fail", last_err

    async def _export_nondoc(self, node_token, obj_token, title, parents):
        """1014 时补查节点类型：附件直链下载、表格按 xlsx 导出。

        返回 (status, detail)；节点确实是普通文档（或查类型本身失败）时返回
        None，交还上层按原来的重试逻辑处理。

        为什么靠错误码触发、不提前给每篇都查：50 篇里通常只有零星几篇是附件
        或表格，全员先查一遍等于白加 50 次请求。失败一次再查，代价只落在
        真正有问题的那几篇上。
        """
        try:
            node = await self.api.get_node(node_token)
        except ApiError:
            # 补查本身失败（网络 / 限流）—— 别把这类错误当成「节点不支持」，
            # 交给外层的退避重试。
            return None
        obj_type = node.get("obj_type")
        if obj_type == OBJ_TYPE_FILE:
            return await self._export_file(node_token, obj_token, title, parents, node)
        if obj_type == OBJ_TYPE_SHEET:
            return await self._export_sheet(node_token, obj_token, title, parents)
        # 不是这两种还回 1014：要么真是 token 不对，要么是没见过的类型。
        # 让它继续走原来的重试，最后一轮把原始错误报出来。
        return None

    async def _export_file(self, node_token, obj_token, title, parents, node):
        """上传的文件：云盘里本来就有原文件，直链下下来，原样落盘。"""
        ext = _file_ext_of(node, title)
        if not ext:
            raise RuntimeError("这是上传的文件，但查不到扩展名，没法决定落盘名字")
        stem = _drop_ext(title, ext)
        out = self.manifest.allocate_path(
            stem, node_token, self.cfg["max_filename_len"], parents, ext,
            wrap=False,  # 原文件没有 `图片和附件/`，不需要折一层文件夹
        )
        self.log.log(f"  标题：{stem} → {self._rel(out)}（上传的原文件，直链下载）")

        tmp = self.manifest.export_dir / f".tmp-{node_token}.{ext}"
        try:
            size = await self.api.download(obj_token, tmp)
            if size <= 0:
                raise RuntimeError("直链下载回来是空文件")
            tmp.replace(out)
        finally:
            tmp.unlink(missing_ok=True)

        self.manifest.record_ok(node_token, title, out, 0, 0, self.sig,
                                kind="file", images=0)
        self.manifest.save()
        return "ok", f"{self._rel(out)}（上传的原文件，{size} 字节）"

    async def _export_sheet(self, node_token, obj_token, title, parents):
        """电子表格：只认 xlsx（pdf / csv / md 都被服务端以 1018 拒掉）。"""
        ext = "xlsx"
        out = self.manifest.allocate_path(
            title, node_token, self.cfg["max_filename_len"], parents, ext,
            wrap=False,
        )
        self.log.log(f"  标题：{title} → {self._rel(out)}（电子表格，导出为 xlsx）")

        ticket = await self.api.export_create(obj_token, ext=ext, src_type="sheet")
        self.log.log(f"  导出任务已提交（ticket={ticket}），等待服务端渲染…")
        file_token = await self.api.wait_export(
            ticket, obj_token,
            interval=self.cfg["poll_interval"],
            timeout=self.cfg["export_timeout"],
            src_type="sheet",
        )

        # xlsx 本身也是 zip（PK 开头），**不能**走 _once 里那套「按文件头判断
        # 要不要解压」的逻辑 —— 那是给 Markdown 附件包用的，解 xlsx 只会得到
        # 一堆 xl/、docProps/ 目录。
        tmp = self.manifest.export_dir / f".tmp-{node_token}.{ext}"
        try:
            size = await self.api.download(file_token, tmp)
            if size <= 0:
                raise RuntimeError("xlsx 下载回来是空文件")
            tmp.replace(out)
        finally:
            tmp.unlink(missing_ok=True)

        self.manifest.record_ok(node_token, title, out, 0, 0, self.sig,
                                kind="sheet", images=0)
        self.manifest.save()
        return "ok", f"{self._rel(out)}（电子表格，{size} 字节）"

    def _rel(self, path):
        """相对导出目录的路径，日志里看着比绝对路径清爽。"""
        try:
            return Path(path).relative_to(self.manifest.export_dir).as_posix()
        except Exception:
            return Path(path).name

    def _min_bytes(self):
        """小体积门槛按格式分。

        Markdown 本来就短：实测知识库根文档「首页」的正文只有 467 字节、
        内容完全正常，套 PDF 那套 3072 会被当成下载失败误杀。
        """
        if self.ext == "pdf":
            return self.cfg["min_pdf_bytes"]
        return self.cfg["min_text_bytes"]

    def _discard(self, out, zipped):
        """残次品别留在磁盘上冒充成品。

        zip 那档要连它解出来的一整包一起清掉 —— 只删正文 .md 的话，
        磁盘上会留下一堆没人认领的图片。
        """
        try:
            if zipped:
                shutil.rmtree(out.parent, ignore_errors=True)
            else:
                out.unlink()
        except Exception:
            pass

    async def _once(self, obj_token, node_token, title, parents=None):
        """走一遍完整导出流程；任何环节不合格都抛异常交给上层重试。"""
        src_type = self.cfg.get("export_src_type", "docx")

        ticket = await self.api.export_create(
            obj_token, ext=self.ext, src_type=src_type,
            need_comment=self.comment, include_file=self.attachment,
        )
        self.log.log(f"  导出任务已提交（ticket={ticket}），等待服务端渲染…")

        file_token = await self.api.wait_export(
            ticket,
            obj_token,
            interval=self.cfg["poll_interval"],
            timeout=self.cfg["export_timeout"],
            src_type=src_type,
        )

        out = self.manifest.allocate_path(
            title, node_token, self.cfg["max_filename_len"], parents, self.ext,
            wrap=self.wrap,
        )
        self.log.log(f"  标题：{title} → {self._rel(out)}")

        # 先下到**临时文件**再决定去处。不能直接下到 out：zip 那档解出来的
        # 正文很可能和 out 同名（都叫 `X.md`），就地解压会一边读一边覆盖自己。
        tmp = self.manifest.export_dir / f".tmp-{node_token}.{self.ext}"
        zipped = False
        try:
            size = await self.api.download(file_token, tmp)
            zipped = is_zip(tmp)
            if zipped:
                # 「带附件的全部内容」= 正文 .md + 图片和附件/，解开成文件夹
                try:
                    out = extract_zip(tmp, out.parent, out.stem)
                except Exception:
                    # 解到一半才炸（磁盘满、包损坏）会留下半个文件夹冒充成品，
                    # 连它一起清掉再往上抛
                    shutil.rmtree(out.parent, ignore_errors=True)
                    raise
                self.log.log(f"  已下载 {size} 字节（附件包），解压中…")
            else:
                self.log.log(f"  已下载 {size} 字节，校验中…")
                tmp.replace(out)
        finally:
            tmp.unlink(missing_ok=True)

        info = inspect_output(out, self.ext, self.cfg["min_pdf_chars"], self._min_bytes())
        info["kind"], info["images"] = "doc", 0

        # Markdown 档特有：整篇是思维导图/画板的文档，飞书导出来就是个空壳
        # （正文只剩一行标题）。**这不是「下坏了」**，重试多少次都一样，
        # 得换条路走。PDF / Word 档不用管：它们本来就把导图当图片嵌在文件里。
        if self.ext == "md" and (self.xmind or self.mindmap):
            blank, md_title = blank_md(out)
            if blank:
                info = await self._mindmap_retry(out, obj_token, md_title or title,
                                                 src_type, info)

        if not info["ok"]:
            self._discard(out, zipped)
            raise RuntimeError(f"{self.ext_label} 校验未通过：{info['reason']}")

        self.log.log(f"  校验通过：{info['reason']}")
        return out, info

    @staticmethod
    def _result(out, ok, reason, kind="doc", images=0, chars=0):
        """拼一个和 inspect_output 同形状的结果。"""
        try:
            size = Path(out).stat().st_size
        except OSError:
            size = 0
        return {"ok": ok, "pages": 0, "chars": chars, "size": size,
                "images": images, "reason": reason, "kind": kind}

    async def _fetch_pdf(self, obj_token, src_type, dst):
        """导一份 PDF 下来，返回 (图片总数, [(扩展名,字节),...], 内容流字节数)。"""
        ticket = await self.api.export_create(obj_token, ext="pdf", src_type=src_type)
        file_token = await self.api.wait_export(
            ticket, obj_token, interval=self.cfg["poll_interval"],
            timeout=self.cfg["export_timeout"], src_type=src_type,
        )
        await self.api.download(file_token, dst)
        return await asyncio.to_thread(read_pdf_images, dst)

    def _keep_pdf(self, keep, out, title, ink, note):
        """把整份 PDF 留下，md 改成链接指向它。

        抠不出「位图」**不等于**这篇是空的 —— 矢量画的画板就一个图片对象都没有，
        可内容是实打实的。这时候唯一诚实的做法是把服务端渲染好的 PDF 原样留住。

        代价是产物形态跟别处不一样（一个 `.md` 加一个 `.pdf`），
        但那是「多一个文件」和「丢一篇内容」之间的选择 —— 选前者。
        """
        out = Path(out)
        head = f"# {title}\n\n" if title else ""
        try:
            out.write_text(f"{head}[{title or out.stem}]({keep.name})\n",
                           encoding="utf-8")
        except OSError as e:
            self.log.log(f"  ! 画板 PDF 已留下但 md 没写成：{e}")
            return self._result(out, False, f"画板 PDF 的 md 没写成：{e}")
        self.log.log(f"  {note}（内容流 {ink} 字节），整份 PDF 留成 {keep.name}")
        fresh = inspect_output(out, self.ext, self.cfg["min_pdf_chars"],
                               self._min_bytes())
        return self._result(out, True, f"画板 → {keep.name}",
                            kind="mindmap", images=0, chars=fresh["chars"])

    async def _xmind_images(self, nodes, extras):
        """把画板里用到的图片下下来，返回 {token: {"data": 字节}}。

        图片是**锦上添花**：下不下来都不影响文字结构，所以这里逐张 try，
        失败只记一行日志。临时目录用完即删，不往导出目录里撒碎文件。
        """
        tokens = [i["token"] for n in nodes for i in (n.get("images") or [])]
        for items in (extras or {}).values():
            tokens.extend(i["token"] for i in items)
        if not tokens:
            return {}

        out = {}
        with tempfile.TemporaryDirectory(prefix="flybook-xmind-") as tmp:
            for token in dict.fromkeys(tokens):
                dst = Path(tmp) / token
                try:
                    size = await self.api.download(token, dst)
                except Exception as e:
                    self.log.log(f"  （画板图片 {token} 下载失败，已跳过："
                                 f"{str(e)[:80]}）")
                    continue
                if size <= 0:
                    continue
                raw = dst.read_bytes()
                # 下载域对不认识的 token 可能回一个 200 的 HTML 错误页，
                # 不验文件头就会把它当 PNG 塞进 XMind，图片位置显示成一片空白。
                if not xmind.is_image(raw):
                    self.log.log(f"  （画板图片 {token} 不是图片格式，已跳过）")
                    continue
                out[token] = {"data": raw}
        return out

    async def _xmind_retry(self, out, obj_token, title, info):
        """把画板节点转成 XMind，写在 Markdown 旁边。返回结果或 None。

        这一步是「导 PDF 抠图」的**首选替代**：同样是从空壳 md 触发，但拿到的
        是一棵可编辑的树，而不是一张位图。转不了（文档里没有画板、画板不是
        思维导图、接口出错）就返回 None，原样交给 PDF 兜底 —— 多这一层绝不
        应该让原本能导的文档导不出来。

        `info["xmind"]` 的两种取值要分清：
          - "none"：确认过没有可转的画板。记进清单，下轮不再白查一遍；
          - 不设  ：查询/转换过程出错（网络、限流、接口变了），下轮还要再试。
            这里之所以不写 "none"，是因为一次临时失败不该被永久记成结论。
        """
        try:
            tokens = await self.api.whiteboard_tokens(obj_token)
        except Exception as e:
            self.log.log(f"  ! 查询文档画板失败：{str(e)[:100]}")
            return None
        if not tokens:
            self.log.log("  文档里没有画板块，改走 PDF 抠图。")
            info["xmind"] = "none"
            return None

        sheets, node_total, image_total = [], 0, 0
        broken = False  # 有没有画板是「没读到」，别把临时故障当成「没有画板」
        for i, token in enumerate(tokens, 1):
            try:
                raw = await self.api.whiteboard_nodes(token)
            except Exception as e:
                self.log.log(f"  ! 读取画板 {token} 失败：{str(e)[:100]}")
                broken = True
                continue
            nodes, extras = xmind.parse_nodes(raw)
            try:
                # 图片先下好，make_sheet 是纯函数、不碰 IO
                images = await self._xmind_images(nodes, extras)
                sheet_title = title if len(tokens) == 1 else f"{title} - 画板{i}"
                sheet, stats = xmind.make_sheet(nodes, sheet_title, extras, images)
            except ValueError as e:
                self.log.log(f"  画板 {token} 未转换：{e}")
                continue
            sheets.append(sheet)
            node_total += stats["nodes"]
            image_total += stats["images"]

        if not sheets:
            # 全部画板都读成功、只是没有思维导图结构 → 这是确定的结论；
            # 有画板没读到 → 可能是网络/限流，不落盘，下轮再试。
            if not broken:
                info["xmind"] = "none"
            return None

        out = Path(out)
        xout = out.with_suffix(".xmind")
        try:
            # 先写 xmind 再改 md：md 是清单里的「正身」，它一改就代表这篇成了。
            # 反过来的话，写 xmind 失败会留下一个指向不存在文件的 md。
            xmind.write_xmind(sheets, xout)
            head = f"# {title}\n\n" if title else ""
            out.write_text(
                f"{head}[{title or out.stem}]({xout.name})\n", encoding="utf-8"
            )
        except Exception as e:
            self.log.log(f"  ! 写 XMind 失败：{str(e)[:100]}")
            try:
                xout.unlink()
            except OSError:
                pass
            return None

        imgs = f" / {image_total} 张图" if image_total else ""
        self.log.log(f"  已转成 XMind：{xout.name}"
                     f"（{len(sheets)} 张画板 / {node_total} 个节点{imgs}）")
        fresh = inspect_output(out, self.ext, self.cfg["min_pdf_chars"],
                               self._min_bytes())
        res = self._result(out, True, f"思维导图 → {xout.name}", kind="xmind",
                           images=image_total, chars=fresh["chars"])
        res["xmind"] = XMIND_MARK
        return res

    async def _mindmap_retry(self, out, obj_token, title, src_type, info):
        """空壳 Markdown 的兜底：改导 PDF，把里面那张导图抠出来。

        产物形态：
            <目录>/<同名>.jpg   抠出来的原图（**原样搬字节**，见 extract_pdf_images）
            <目录>/<同名>.md    改写成引用它的 Markdown
        抠不到位图时整份 PDF 留下，md 改成链接指向它。

        md 为什么要改写：不改的话文件夹里躺着一个 10 字节的空壳 md，内容孤零零
        在旁边，浏览时看不到、进笔记库也搜不到。改完 `# 标题` + 引用才是
        「这份文档长这样」的诚实记录。

        ⚠️ **没有位图时一律把 PDF 留下，不判「空文档」。** 这条是实测改出来的：

        同一篇画板文档，连着导三次 PDF，一次给 30 KB（内容流 16 KB，里面有
        194 段曲线、19 条描边 —— 真画了图），另两次只给 6 KB（内容流 860 字节，
        就一个白底加标题）。**服务端的渲染会时好时坏。** 于是「没抠出图 + 内容流
        很小 ⇒ 这篇是空的」这个推理是错的：它可能只是**这一版没渲染出来**。
        早先按这个推理把「接口文档」记成「空文档」并放行了，实际丢了一张图。

        所以现在：抠不到位图就多导几轮，留内容流最大的那版，并且一律把 PDF 留下
        （多花的配额只落在画板这类文档上 —— 有真图的思维导图第一轮就收工了）。
        误判成「多一个没用的 PDF」只是多占几 KB，误判成「空」是丢内容 ——
        这边错得起，那边错不起。
        """
        self.log.log("  Markdown 正文只有标题（整篇是一张思维导图）。")
        # 首选 XMind：结构完整、能编辑。失败/不适用再走下面的 PDF 抠图。
        if self.xmind:
            self.log.log("  先试转 XMind…")
            got = await self._xmind_retry(out, obj_token, title, info)
            if got is not None:
                return got
        if not self.mindmap:
            return info
        self.log.log("  改导 PDF 抠图…")
        tmp = self.manifest.export_dir / f".tmp-{Path(out).stem}-mindmap.pdf"
        keep = Path(out).parent / f"{Path(out).stem}.pdf"
        images, best_ink = [], -1
        try:
            for round_no in range(_PDF_ROUNDS):
                total, got, ink = await self._fetch_pdf(obj_token, src_type, tmp)
                if total is None:
                    self.log.log("  ! PDF 解析失败，保持原样交给上层重试。")
                    return info
                if got:
                    images = got
                    break
                # 没位图：把这一版留着（每轮都比一下内容流，留最全的那版）
                if ink > best_ink:
                    best_ink = ink
                    tmp.replace(keep)
                if round_no + 1 < _PDF_ROUNDS:
                    self.log.log(f"  这一版没抠到位图（内容流 {ink} 字节），"
                                 f"再导一次 —— 只留内容最全的那版…")
        except Exception as e:
            self.log.log(f"  ! 思维导图兜底没成：{e}")
            return info
        finally:
            # 留下的是 keep，tmp 每轮重新下载，这里删的永远是最后那份临时件
            tmp.unlink(missing_ok=True)

        if not images:
            res = self._keep_pdf(keep, out, title, best_ink, "没抠到位图")
            # 把「查过、确实没有可转画板」这个结论带到清单里，
            # 否则下一轮还会为同一篇文档再查一次
            res["xmind"] = info.get("xmind", "")
            return res

        if keep.exists():
            keep.unlink()   # 有真图了，那份 PDF 就没必要留着
        out = Path(out)
        written = []
        for i, (ext, data) in enumerate(images, 1):
            name = f"{out.stem}.{ext}" if i == 1 else f"{out.stem}_{i}.{ext}"
            (out.parent / name).write_bytes(data)
            written.append(name)

        head = f"# {title}\n\n" if title else ""
        body = "\n".join(f"![{title or out.stem}]({n})" for n in written)
        out.write_text(f"{head}{body}\n", encoding="utf-8")

        self.log.log(f"  导图已抠出：{'、'.join(written)}")
        fresh = inspect_output(out, self.ext, self.cfg["min_pdf_chars"],
                               self._min_bytes())
        res = self._result(out, True, f"思维导图 → {'、'.join(written)}",
                           kind="mindmap", images=len(written),
                           chars=fresh["chars"])
        res["xmind"] = info.get("xmind", "")
        return res
