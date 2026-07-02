"""KOS 出图引擎(直出版,固定 3:4 画布,不裁切/不翻转/不变形)。

三类成品:
  主图     —— 原图直出(不经本模块,调用方直接拷贝原文件字节,零改动)
  2合1横版 —— 两张横版图【上下】各占一半,拼进 3:4 画布(等比缩放 + 居中留白,不裁切)
  4合1竖版 —— 四张竖版图【田字】2×2,拼进 3:4 画布(等比缩放 + 居中留白,不裁切)

成品统一为小红书竖图 3:4(默认 1080×1440)。为保证"不裁切、不变形",
图片按 contain(整张放进单元格)方式缩放,比例不合处以白底居中留白填充。
"""
from PIL import Image

# 小红书竖图 3:4
DEFAULT_CANVAS = (1080, 1440)
WHITE = (255, 255, 255)


def _open_rgb(path):
    return Image.open(path).convert("RGB")


def _fit_contain(img, cw, ch, bg=WHITE):
    """把整张图等比缩放到能放进 cw×ch(不裁切、不变形),再居中贴到 cw×ch 的底图上。"""
    iw, ih = img.size
    scale = min(cw / iw, ch / ih)
    nw, nh = max(1, round(iw * scale)), max(1, round(ih * scale))
    resized = img.resize((nw, nh), Image.LANCZOS)
    cell = Image.new("RGB", (cw, ch), bg)
    cell.paste(resized, ((cw - nw) // 2, (ch - nh) // 2))
    return cell


def stack_vertical(paths, out_path, canvas=DEFAULT_CANVAS, gutter=0, bg=WHITE, quality=92):
    """2合1:两张横版图上下各占一半,拼进固定 3:4 画布。不裁切、不翻转、不变形。"""
    if len(paths) != 2:
        raise ValueError(f"2合1 需要 2 张,收到 {len(paths)}")
    cw, ch = canvas
    cell_h = (ch - gutter) // 2
    base = Image.new("RGB", (cw, ch), bg)
    ys = [0, cell_h + gutter]
    for p, y in zip(paths, ys):
        base.paste(_fit_contain(_open_rgb(p), cw, cell_h, bg), (0, y))
    base.save(out_path, "JPEG", quality=quality, optimize=True)
    return out_path


def grid_2x2(paths, out_path, canvas=DEFAULT_CANVAS, gutter=0, bg=WHITE, quality=92):
    """4合1:四张竖版图 2×2 田字,拼进固定 3:4 画布。不裁切、不翻转、不变形。"""
    if len(paths) != 4:
        raise ValueError(f"4合1 需要 4 张,收到 {len(paths)}")
    cw, ch = canvas
    cell_w = (cw - gutter) // 2
    cell_h = (ch - gutter) // 2
    base = Image.new("RGB", (cw, ch), bg)
    cells = [(0, 0), (cell_w + gutter, 0), (0, cell_h + gutter), (cell_w + gutter, cell_h + gutter)]
    for p, (cx, cy) in zip(paths, cells):
        base.paste(_fit_contain(_open_rgb(p), cell_w, cell_h, bg), (cx, cy))
    base.save(out_path, "JPEG", quality=quality, optimize=True)
    return out_path
