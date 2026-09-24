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
import logging
import re
import shutil
import stat
import zipfile
from pathlib import Path

from .config import ext_of
from .manifest import opts_sig

# 日志里说人话用的格式名（`docx` 说成 Word，省得看日志的人愣一下）
EXT_LABEL = {"pdf": "PDF", "docx": "Word", "md": "Markdown"}

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

    async def export(self, node_token, obj_token, title, parents=None):
        """导出单篇。返回 (status, detail)，status ∈ {ok, skip, fail}。

        parents 是祖先标题链，用来决定这篇落在哪个子目录（见 manifest.allocate_path）。
        """
        skip = self.cfg.get("skip_existing", True)
        # 除了「下过没下过」，还得看「位置和选项对不对」：目录规则变了、或者
        # 换了格式/评论选项时，老文件还在老地方，只比 is_done 会把它们全放过。
        if self.manifest.is_done(node_token, skip) and self.manifest.is_current(
            node_token, parents, self.sig, self.cfg["max_filename_len"],
            wrap=self.wrap, title=title,
        ):
            rec = self.manifest.get(node_token) or {}
            return "skip", rec.get("file", "")

        if not title:
            title = (self.manifest.get(node_token) or {}).get("title") or node_token

        attempts = max(1, self.cfg["max_retry"])
        backoff = self.cfg["retry_backoff"]
        last_err = "未知错误"

        for attempt in range(1, attempts + 1):
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
                self.log.log(f"  第 {attempt}/{attempts} 次失败：{last_err}")
                if attempt < attempts:
                    wait = backoff * (2 ** (attempt - 1))
                    self.log.log(f"  等待 {wait:.0f} 秒后重试…")
                    await asyncio.sleep(wait)
                continue

            self.manifest.record_ok(node_token, title, out, info["pages"],
                                    info["chars"], self.sig)
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
        if not info["ok"]:
            self._discard(out, zipped)
            raise RuntimeError(f"{self.ext_label} 校验未通过：{info['reason']}")

        self.log.log(f"  校验通过：{info['reason']}")
        return out, info
