"""画板节点 → XMind 文件。

为什么要这个模块：整篇是画板（思维导图）的文档，Markdown 导出来只有
一行标题。早先的兜底是导 PDF 再抠出那张位图（见 exporter._mindmap_retry），
图能用，但**结构全丢了** —— 搜不到、改不了、也没法折叠。画板节点本身就是
一棵树，直接转成 XMind 才是这份内容本来的样子。

⚠️ 输入有**两种格式**，字段长得完全不一样，别拿一种去套另一种：

1. 网页内部接口 `/space/api/whiteboard/block`（本项目走这条）：
       {"nodes": [{"id": "z2:7", "info": {"textV2": {"text": "..."},
                                          "mindMap": {"parentId": "z1:1"},
                                          "baseV2": {"width": 80, "height": 40}}}]}
2. lark-cli `whiteboard +query --output_as raw` 的开放平台原生格式
   （原 wb2xmind.py 吃的就是它）：
       [{"id": "...", "type": "mind_node_text", "text": {"text": "..."},
         "mind_map": {"parent_id": "..."}, "width": 80, "height": 40}]

`parse_nodes` 把两种都归一成内部结构，后面的建树与写包不再关心来源。

图片：原脚本靠 lark-cli 下载图片再 base64 内嵌。这里不依赖外部命令，
由调用方把「token → 字节」传进来（exporter 用 api.download 下好）。
下载失败就跳过图片，**不因此判整篇失败** —— 文字结构才是主体。
"""

import base64
import json
import os
import re
import uuid
import zipfile
from pathlib import Path

# 空文本的占位，和原脚本保持一致
EMPTY_TEXT = "(无标题)"

# 防御异常数据：正常画板深度远小于这个值，循环引用才会撞上
_MAX_DEPTH = 1000

# 文件头 → MIME。XMind 的 data URI 必须带对类型，否则图片不显示。
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),  # 还要看第 8~12 字节是不是 WEBP，见 mime_of
)


def is_image(data):
    """这段字节是不是认识的图片？用来拦掉「下载回来其实是个 HTML 错误页」。"""
    return any(
        data.startswith(head)
        and not (mime == "image/webp" and data[8:12] != b"WEBP")
        for head, mime in _MAGIC
    )


def mime_of(data):
    """按文件头猜图片类型。认不出就按 PNG 处理（XMind 对它有兜底）。"""
    for head, mime in _MAGIC:
        if data.startswith(head):
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    return "image/png"


def _unwrap(raw):
    """容忍外层包一层接口响应（{"code":0,"data":{...}}）。"""
    if isinstance(raw, dict):
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        if isinstance(data.get("nodes"), list):
            return data["nodes"]
    if isinstance(raw, list):
        return raw
    return []


def _text_of(value):
    """text 可能是字符串，也可能是 {"text": "..."}。"""
    if isinstance(value, dict):
        value = value.get("text")
    return str(value or "").strip()


# 画板里的图片有两种藏法：
#   1. 独立图片节点：info.image / info.imageV2 / 原生格式的 image.token；
#   2. **形状填充图**（实测最常见）：图片被当成某个形状的填充色，
#      藏在 info.fillV2.fillStyleList.data[].imageFillItem 里，真正的
#      token 在 imageFillItem.resource.key（imageUrl 里也能抠出来）。
# 只看第 1 种会漏掉绝大多数图片 —— 用户看到的就是「导图里图片全没了」。
_RE_DOWNLOAD = re.compile(r"/download/(?:preview|all)/([^/?#]+)")


def _token_from_url(url):
    """从画板图片的下载地址里抠 token。"""
    m = _RE_DOWNLOAD.search(str(url or ""))
    return m.group(1) if m else ""


def _size_of(info, raw):
    """节点尺寸：内部格式在 info.baseV2，原生格式在顶层。"""
    base = info.get("baseV2") if isinstance(info, dict) else None
    if not isinstance(base, dict):
        base = raw if isinstance(raw, dict) else {}
    try:
        w = float(base.get("width") or 0) or 200
        h = float(base.get("height") or 0) or 150
    except (TypeError, ValueError):
        w, h = 200, 150
    return w, h


def _images_of(info, raw):
    """一个节点自身携带的图片（独立图片字段 + 填充图）。"""
    out, seen = [], set()

    def add(token, width=None, height=None):
        token = str(token or "").strip()
        if not token or token in seen:
            return
        seen.add(token)
        out.append({"token": token,
                    "width": width or w, "height": height or h})

    w, h = _size_of(info, raw)
    img = info.get("image") or info.get("imageV2")
    if isinstance(img, dict):
        add(img.get("token"), img.get("width"), img.get("height"))

    fills = dig_path(info, "fillV2", "fillStyleList", "data")
    for item in fills if isinstance(fills, list) else []:
        fill = item.get("imageFillItem") if isinstance(item, dict) else None
        if not isinstance(fill, dict):
            continue
        res = fill.get("resource") if isinstance(fill.get("resource"), dict) else {}
        add(res.get("key") or _token_from_url(fill.get("imageUrl")))
    return out


def _nested_images(raw, depth=0):
    """复合节点 children 里的图片。

    画板把「思维导图节点」和「它上面挂的图片/形状」拆开了：图片不在顶层
    nodes 里，而是父节点的 children 数组里，而且可能再嵌一层。建树只认
    mindMap.parentId，但**图片必须从这里捞**，否则带截图的节点就只剩文字。
    """
    if depth >= 20:
        return []
    kids = raw.get("children")
    if not isinstance(kids, list):
        return []
    out = []
    for child in kids:
        if not isinstance(child, dict):
            continue
        info = child.get("info") if isinstance(child.get("info"), dict) else {}
        out.extend(_images_of(info, child))
        out.extend(_nested_images(child, depth + 1))
    return out


def dig_path(obj, *keys):
    """按路径取嵌套字典，断了返回 None。"""
    cur = obj
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _one(n):
    """把一个原始节点归一化。认不出来返回 None。

    返回 (节点, 是否思维导图节点)。图片节点没有文字，会作为附件挂到父节点上，
    由 parse_nodes 单独收走。
    """
    info = n.get("info") if isinstance(n.get("info"), dict) else None
    if info is not None:
        # —— 网页内部接口格式 ——
        text = _text_of(info.get("textV2"))
        mm = info.get("mindMap") if isinstance(info.get("mindMap"), dict) else {}
        parent = mm.get("parentId") or mm.get("parent_id") or ""
        width, height = _size_of(info, n)
        # 自身图片 + 复合子节点里的图片，都算这个节点的内容
        images = _images_of(info, n) + _nested_images(n)
        is_mind = bool(mm)
    else:
        # —— lark-cli / 开放平台原生格式 ——
        text = _text_of(n.get("text"))
        mm = n.get("mind_map") or n.get("mind_map_node") or {}
        if not isinstance(mm, dict):
            mm = {}
        # 原生格式里图片节点没有 mind_map，父节点写在顶层 parent_id 上
        # （原 wb2xmind.py 就是这么取图片归属的）。
        parent = mm.get("parent_id") or n.get("parent_id") or ""
        width = n.get("width") or 200
        height = n.get("height") or 150
        img = n.get("image") if isinstance(n.get("image"), dict) else {}
        token = img.get("token")
        images = ([{"token": str(token), "width": width, "height": height}]
                  if token else [])
        # 原生格式用 type 区分：图片节点单独一种，不参与建树
        is_mind = bool(mm) or n.get("type") in (
            "mind_node", "mind_node_text", "mind_map",
        )

    nid = str(n.get("id") or "").strip()
    if not nid:
        return None
    return (
        {
            "id": nid,
            "text": text,
            "parent_id": str(parent or "").strip(),
            "width": width,
            "height": height,
            "images": images,
            "mind": is_mind,
        },
        bool(images) and not text,
    )


def parse_nodes(raw):
    """归一化节点列表。

    返回 (nodes, images_by_parent)：
      nodes            —— 参与建树的节点
      images_by_parent —— {父节点 id: [{"token", "width", "height"}, ...]}

    图片节点（有 token、没文字）不建成 topic，而是挂到父节点上，这是原脚本
    的行为。它可能排在父节点前面或后面，所以先收到表里，建 topic 时再取。
    """
    nodes, images_by_parent = [], {}
    for n in _unwrap(raw):
        if not isinstance(n, dict):
            continue
        got = _one(n)
        if got is None:
            continue
        node, is_image = got
        if is_image:
            parent = node["parent_id"]
            if parent:
                images_by_parent.setdefault(parent, []).extend(node["images"])
            continue
        nodes.append(node)
    return nodes, images_by_parent


def _tree(nodes):
    """建树，返回 (根节点列表, {父 id: [子节点]})。

    - 父节点不存在（或指向自己）的节点按根处理 —— 别为了「结构完整」
      把它丢掉，画板数据里偶尔有断链。
    - 子节点保持接口给的顺序。内部接口的顺序实测就是思维导图里的顺序，
      原脚本也是照原序建的，别自作主张按 id 或坐标重排。
    """
    by_id = {n["id"]: n for n in nodes}
    children, roots = {}, []
    for n in nodes:
        pid = n["parent_id"]
        if pid and pid != n["id"] and pid in by_id:
            children.setdefault(pid, []).append(n)
        else:
            roots.append(n)
    return roots, children


def _topic_id(nid, used):
    """XMind 里的 topic id。保持可读（带上画板节点 id），撞了就加序号。"""
    base = "wb-" + re.sub(r"[^0-9A-Za-z_-]", "_", nid)
    cand, i = base, 1
    while cand in used:
        i += 1
        cand = f"{base}-{i}"
    used.add(cand)
    return cand


def _image_obj(img, images):
    """把一张图片做成 XMind 的 image 对象；没下到字节就返回 None。"""
    data = images.get(img.get("token"))
    if not data or not data.get("data"):
        return None
    raw = data["data"]
    mime = data.get("mime") or mime_of(raw)
    return {
        "src": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}",
        "width": int(float(img.get("width") or 200)),
        "height": int(float(img.get("height") or 150)),
    }


def _topic(node, children, extras, images, used, stats, depth=0, path=None):
    """递归生成 XMind topic。

    path 记录当前这条链上走过的 id：父指针成环时（A→B→A）只靠深度上限会
    白递归一千层，这里直接截断，避免把一份坏数据放大成巨型文件。
    """
    path = path or set()
    text = node["text"] or EMPTY_TEXT
    topic = {
        "id": _topic_id(node["id"], used),
        "class": "topic",
        "title": text,
        "attributedTitle": [{"text": text}],
    }
    stats["nodes"] += 1

    # 节点自身的图 + 复合子节点里的图 + 单独挂上来的图片节点。
    # XMind 一个 topic 只放得下一张图：第一张放 topic 上，其余做成「(图片)」
    # 子 topic，宁可多两个子节点，也不把图丢掉。
    usable, seen_img = [], set()
    for img in list(node.get("images") or []) + list(extras.get(node["id"]) or []):
        token = img.get("token")
        if token in seen_img:
            continue
        seen_img.add(token)
        obj = _image_obj(img, images)
        if obj:
            usable.append(obj)
    img_topics = []
    if usable:
        topic["image"] = usable[0]
        stats["images"] += 1
        for j, obj in enumerate(usable[1:], 2):
            text = "(图片)"
            img_topics.append({
                "id": _topic_id(f"{node['id']}-img{j}", used),
                "class": "topic",
                "title": text,
                "attributedTitle": [{"text": text}],
                "image": obj,
            })
            stats["images"] += 1

    attached = []
    if depth < _MAX_DEPTH and node["id"] not in path:
        kids = children.get(node["id"]) or []
        if kids:
            path = path | {node["id"]}
            attached = [
                _topic(k, children, extras, images, used,
                       stats, depth + 1, path)
                for k in kids
            ]
    attached.extend(img_topics)
    if attached:
        topic["children"] = {"attached": attached}
    return topic


def make_sheet(nodes, title, images_by_parent=None, images=None):
    """把一棵画板树做成一张 XMind sheet。返回 (sheet, stats)。

    nodes 必须是 parse_nodes 的输出。没有节点、或一个思维导图节点都没有时
    抛 ValueError —— 调用方据此走 PDF 兜底，别把一张自由画板硬转成空导图。
    """
    images_by_parent = images_by_parent or {}
    images = images or {}
    if not nodes:
        raise ValueError("画板里没有节点")
    if not any(n.get("mind") for n in nodes):
        raise ValueError("画板不是思维导图结构")

    roots, children = _tree(nodes)
    if not roots:
        raise ValueError("画板节点成环，找不到根节点")

    stats = {"nodes": 0, "images": 0, "roots": len(roots)}
    used = set()
    if len(roots) == 1:
        root_topic = _topic(roots[0], children, images_by_parent,
                            images, used, stats)
    else:
        # 多个根：合成一个虚拟根，免得丢掉其中几棵树。原脚本只留最后一个根，
        # 多根画板会静默少内容。
        root_topic = {
            "id": _topic_id("root", used),
            "class": "topic",
            "title": title or "画板",
            "attributedTitle": [{"text": title or "画板"}],
            "children": {
                "attached": [
                    _topic(r, children, images_by_parent, images,
                           used, stats)
                    for r in roots
                ]
            },
        }

    sheet = {
        "id": str(uuid.uuid4()),
        "class": "sheet",
        "title": title or "Sheet 1",
        "rootTopic": root_topic,
    }
    return sheet, stats


def write_xmind(sheets, path):
    """把若干 sheet 写成 .xmind（本质是个装三份 JSON 的 zip）。

    先写临时文件再替换：中途失败不会留下一个半截 .xmind 冒充成品。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp-{path.name}")
    metadata = {
        "dataStructureVersion": "2",
        "creator": {"name": "flybook", "version": "1.0.0"},
        "layoutEngineVersion": "3",
    }
    manifest = {"file-entries": {"content.json": {}, "metadata.json": {}}}
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("content.json", json.dumps(sheets, ensure_ascii=False))
            zf.writestr("metadata.json", json.dumps(metadata, ensure_ascii=False))
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return path


def convert(raw, out_path, title="Sheet 1", images=None):
    """一步到位：原始节点 → 一个 .xmind。返回 stats。

    供 wb2xmind.py 这类命令行用；流水线里用 parse_nodes + make_sheet + write_xmind，
    因为一份文档可能有多个画板、要合成一个多 sheet 的文件。
    """
    nodes, extras = parse_nodes(raw)
    sheet, stats = make_sheet(nodes, title, extras, images)
    write_xmind([sheet], out_path)
    return stats
