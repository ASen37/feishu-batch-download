"""增量清单：记录每篇文档的导出结果，支撑跳过已导出与断点续传。

以 node_token 而非标题做主键：文档改名后仍认得出是同一篇（覆盖旧文件），
标题重名时也能正确区分（加序号），从根上消除旧版 `_2`、`_3` 不断堆积的问题。

v2 起 `file` 存的是**相对导出目录的路径**（用 / 分隔），以支持按飞书的目录结构
分文件夹存放。v1 的旧记录存的是纯文件名，两种都能读（见 _resolve）。
"""

import hashlib
import json
import re
import time
from pathlib import Path

VERSION = 2

# 要替换成下划线的字符，分三类：
#   1) Windows 文件名非法： \ / : * ? " < > |  以及控制字符（单独处理）
#   2) 点名要处理的符号：  、 & @ # $ % ^ ( ) [ ] { } ; , ' . 和反引号
#   3) 上面这些的**全角写法**
# 规则很直白：半角在该换的名单里，对应的全角就一起换。
# 方括号只收 ［］（全角方括号），不收 【】—— 后者是中文标题里常见的装饰，
# 换了反而难认；《》同理保留。
_ASCII_SPECIAL = "\\/:*?\"<>|&@#$%^()[]{};,'.`"
_FULLWIDTH_SPECIAL = "、。，；：？（）［］｛｝＜＞／＼｜＆＾＄＃＠％＊“”‘’｀"
_RE_SPECIAL = re.compile("[" + re.escape(_ASCII_SPECIAL + _FULLWIDTH_SPECIAL) + "]")
_RE_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# 连续下划线，以及它两侧的空白： "6_ 接口…" → "6_接口…"
_RE_TIGHT = re.compile(r"\s*_[\s_]*")

# Windows 保留设备名，撞上了根本建不出文件
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def safe_name(title, max_len=120):
    """把标题转成 Windows 安全的文件/目录名。

    实际效果举例：

        第1章-Java基础-基础语法、流程控制、方法  →  第1章-Java基础-基础语法_流程控制_方法
        6. 接口文档-线索管理                     →  6_接口文档-线索管理
        第4章-容器-Array、String&StringBuilder   →  第4章-容器-Array_String_StringBuilder
        第5章-Stream、异常、Maven                →  第5章-Stream_异常_Maven

    注意点号也在替换名单里，所以这个函数只用来处理**标题**；
    扩展名（.pdf）由调用方拼上去，不经过这里。
    """
    name = _RE_CONTROL.sub("", title or "")
    name = _RE_SPECIAL.sub("_", name)
    name = _RE_TIGHT.sub("_", name)
    # 首尾的下划线、点、空格都不能留：结尾的点 Windows 直接不认，
    # 开头的点在类 Unix 下是隐藏文件。
    name = name.strip().strip("._").strip()
    if len(name) > max_len:
        name = name[:max_len].strip().strip("._").strip()
    if not name:
        return f"doc_{int(time.time() * 1000)}"
    if name.upper() in _RESERVED:
        name += "_"
    return name


def opts_sig(ext, comment=False, attachment=True):
    """导出选项的签名，存进清单，用来判断「记录里那份」是不是按**现在这套选项**导的。

    光看文件在不在是不够的：同一个路径，带评论和不带评论是两个不同的产物。

    格式各自的选项互不相干，所以只留该格式真正用得上的那位：
    Word/PDF 看评论，Markdown 看附件。**另一个开关不参与签名** ——
    否则导 Markdown 时顺手调一下评论开关，就会让 41 篇 PDF 全部重下。
    """
    if ext == "md":
        return f"md|-|{'a' if attachment else '-'}"
    return f"{ext}|{'c' if comment else '-'}|-"


# 去重时加的尾巴： "作业" 撞名了就叫 "作业_2"、再撞 "作业_3"
_RE_DEDUP = re.compile(r"_\d+$")


def undedup(name):
    """去掉去重加的 `_2`/`_3` 尾巴，还原「本来该叫什么」。

    比对「文件放对位置没有」时必须先剥掉它：去重后的文件夹叫 `作业_2`，
    但按规则算出来的期望值是 `作业`，不剥就会永远判「位置不对」，
    于是每次运行都重下一遍。
    """
    return _RE_DEDUP.sub("", name)


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Manifest:
    def __init__(self, path, export_dir):
        self.path = Path(path)
        self.export_dir = Path(export_dir)
        self.docs = {}
        self._reserved = {}  # 本次运行中已分配的相对路径 -> token
        self._load()
        # 有清单才有「外来文件」的判断依据；清单不存在（首次运行或主人删了它）
        # 说明没有基线，此时复用文件名直接覆盖，避免全量重跑时堆出一片 _2。
        self._has_baseline = bool(self.docs)

    # ---------- 读写 ----------

    def _load(self):
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(data, dict) and isinstance(data.get("docs"), dict):
            self.docs = data["docs"]

    def save(self):
        payload = {"version": VERSION, "updated_at": now_str(), "docs": self.docs}
        try:
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    # ---------- 查询 ----------

    def get(self, token):
        return self.docs.get(token)

    def is_done(self, token, skip_existing=True):
        """已成功导出、且文件仍在且大小未变时返回 True。"""
        if not skip_existing:
            return False
        rec = self.docs.get(token)
        if not rec or rec.get("status") != "ok":
            return False
        f = self._resolve(rec.get("file", ""))
        if f is None or not f.exists() or f.stat().st_size != rec.get("size"):
            return False
        return True

    def is_current(self, token, parents, sig, max_len=120, wrap=False, title=""):
        """清单里那份，跟这次要导的**位置和选项**都对得上吗？

        is_done 只比对清单里记的那条路径。下面几件事变了，路径却可能一模一样，
        于是老文件一路放行、纹丝不动，看起来就像「改了没生效」：

        - **位置**：目录规则改过之后（父文档从「和文件夹平级」改成「折进自己的
          文件夹」），老记录指向老位置，老文件也还在。
        - **选项**：`downloads/首页.pdf` 这个路径，带评论和不带评论**是同一个**。
          导过不带评论的再选「带评论」，文件在、大小也对 —— 静默跳过。
        - **形态**：Markdown 带附件那档，同一篇文档会在「单独一个 .md」和
          「装进同名文件夹」之间切换（有没有附件决定飞书给不给 zip）。

        wrap=True 表示这次要求文档折进以自己命名的文件夹里（见 allocate_path）。
        比目录时两边都先剥掉去重尾巴：文件夹可能因撞名被叫成 `作业_2`，
        而期望值算出来是 `作业`，不剥就会每次都判「位置不对」、永远重下。
        """
        rec = self.docs.get(token)
        if not rec or not rec.get("file"):
            return False
        want = [safe_name(p, max_len) for p in (parents or [])]
        if wrap:
            want.append(safe_name(title or rec.get("title", ""), max_len))
        got = list(Path(rec["file"]).parent.parts)
        if [undedup(x) for x in want] != [undedup(x) for x in got]:
            return False
        legacy = opts_sig(Path(rec["file"]).suffix.lstrip(".") or "pdf")
        return (rec.get("opts") or legacy) == sig

    def _resolve(self, name):
        """把记录里的 file 还原成绝对路径。

        v2 存相对路径（可能带子目录），v1 存纯文件名 —— Path 两种情况都能拼，
        所以这里不再取 .name（那会把子目录丢掉）。
        """
        if not name:
            return None
        p = Path(name)
        return p if p.is_absolute() else self.export_dir / p

    # ---------- 文件名分配 ----------

    def allocate_path(self, title, token, max_len=120, parents=None, ext="pdf",
                      wrap=False):
        """分配输出路径，必要时建好目录。

        parents 是祖先标题链（**不含根文档**），用来还原飞书的目录结构：

            parents=["第1章-Java基础-基础语法…"], title="作业", ext="md"
                → downloads/第1章-Java基础-基础语法…/作业.md

        wrap=True 表示**把文档自己也折进一层同名文件夹**（文件跟着文件夹同名）：

            parents=["第1章-Java基础-基础语法…"], title="作业", ext="md"
                → downloads/第1章-Java基础-基础语法…/作业/作业.md

        什么时候要 wrap：Markdown 选「带附件的全部内容」时，飞书给的是一个
        zip（正文 .md + `图片和附件/`）。那个附件目录名字是**固定的**，
        两篇文档落在同一个父目录下就会把图片**互相覆盖**，所以每篇必须各占
        一个文件夹。这跟 collector.build_paths 里「有子文档的父文档折进自己
        文件夹」是同一个思路 —— 那边是内容需要，这边是附件需要。

        ext 由导出格式决定（pdf / docx / md）。标题里的点会被换成下划线，
        所以扩展名一定是这里拼上去的那个，不会串味。

        去重规则：
        - 同名文件属于同一个 token     → 复用原名（增量重导，覆盖旧文件）
        - 同名文件属于别的 token       → 加序号，避免互相覆盖
        - 同名文件在磁盘上、清单里有基线 → 视为外来文件，加序号，不擅自覆盖

        wrap 模式下序号加在**文件夹**上（`作业_2/作业_2.md`）：两篇同名文档
        的候选路径本来就完全相同，交给下面同一套 taken 判断即可，不用另写一套。

        分配到的名字会立刻登记进 _reserved：并发下两个 worker 可能在同一轮里
        都还没写盘，只靠清单会双双拿到同一个文件名、互相覆盖。
        """
        rel_dir = Path(*[safe_name(p, max_len) for p in parents]) if parents else Path()
        owners = {r.get("file"): t for t, r in self.docs.items() if r.get("file")}
        owners.update(self._reserved)

        def taken(rel):
            key = rel.as_posix()
            holder = owners.get(key)
            if holder is not None:
                return holder != token
            if not self._has_baseline:
                return False
            return (self.export_dir / rel).exists()

        base = safe_name(title, max_len)
        pick = None
        for i in range(1, 1000):
            stem = base if i == 1 else f"{base}_{i}"
            cand = rel_dir / stem / f"{stem}.{ext}" if wrap else rel_dir / f"{stem}.{ext}"
            if not taken(cand):
                pick = cand
                break
        if pick is None:  # 999 个都撞了，时间戳兜底
            stem = f"{base}_{int(time.time())}"
            pick = rel_dir / stem / f"{stem}.{ext}" if wrap else rel_dir / f"{stem}.{ext}"

        out = self.export_dir / pick
        out.parent.mkdir(parents=True, exist_ok=True)
        self._reserved[pick.as_posix()] = token
        return out

    # ---------- 写入结果 ----------

    def _rel(self, path):
        """存相对路径，跨机器搬运清单也不会失效。"""
        try:
            return Path(path).resolve().relative_to(self.export_dir.resolve()).as_posix()
        except Exception:
            return Path(path).name

    def record_ok(self, token, title, path, pages, chars, opts=""):
        path = Path(path)
        try:
            size, digest = path.stat().st_size, sha1_of(path)
        except Exception:
            size, digest = 0, ""
        self.docs[token] = {
            "title": title or "",
            "file": self._rel(path),
            # 导出选项签名：换格式或换评论/附件选项时靠它发现「这份是旧的」
            "opts": opts,
            "size": size,
            "sha1": digest,
            "pages": pages,
            "chars": chars,
            "exported_at": now_str(),
            "status": "ok",
        }

    def record_fail(self, token, title, error):
        prev = self.docs.get(token) or {}
        self.docs[token] = {
            "title": title or prev.get("title", ""),
            "file": prev.get("file", ""),
            "exported_at": now_str(),
            "status": "failed",
            "error": str(error)[:300],
        }
