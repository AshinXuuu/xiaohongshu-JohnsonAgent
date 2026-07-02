"""KOS 出图引擎(直出版,不做任何裁切/翻转/随机)。

三类成品:
  主图     —— 原图直出(不经本模块,调用方直接拷贝原文件字节,零改动)
  2合1横版 —— 两张横版图等宽后【上下】拼接(不裁切、不翻转、不变形)
  4合1竖版 —— 四张竖版图【田字】2×2 拼接(等宽,行内按最高对齐、居中留白,不裁切不变形)

设计原则:只做等比缩放与画布拼接;绝不裁切、翻转、拉伸变形。
输出重编码为高质量 JPEG(仅拼图成品;主图不经这里,保持原样直出)。
"""
from PIL import Image

WHITE = (255, 255, 255)


def _open_rgb(path):
    return Image.open(path).convert("RGB")


def _fit_width(img, w):
    """等比缩放到目标宽度 w,返回缩放后的图(高度随比例,不裁切不变形)。"""
    iw, ih = img.size
    if iw == w:
        return img
    nh = max(1, round(ih * w / iw))
    return img.resize((w, nh), Image.LANCZOS)


def stack_vertical(paths, out_path, width=1080, gutter=0, bg=WHITE, quality=92):
    """两张(或多张)横版图等宽后上下拼接。不裁切、不翻转。
    画布宽=width,高=各图缩放后高度之和(+间隙)。"""
    imgs = [_fit_width(_open_rgb(p), width) for p in paths]
    total_h = sum(im.size[1] for im in imgs) + gutter * (len(imgs) - 1)
    canvas = Image.new("RGB", (width, total_h), bg)
    y = 0
    for im in imgs:
        canvas.paste(im, (0, y))
        y += im.size[1] + gutter
    canvas.save(out_path, "JPEG", quality=quality, optimize=True)
    return out_path


def grid_2x2(paths, out_path, cell_w=540, gutter=0, bg=WHITE, quality=92):
    """四张竖版图 2×2 田字拼接。每张等宽缩放到 cell_w(不裁切不变形),
    统一单元格高度=四张缩放后的最大高度,较矮的居中留白。"""
    if len(paths) != 4:
        raise ValueError(f"4合1 需要 4 张,收到 {len(paths)}")
    imgs = [_fit_width(_open_rgb(p), cell_w) for p in paths]
    cell_h = max(im.size[1] for im in imgs)
    W = cell_w * 2 + gutter
    H = cell_h * 2 + gutter
    canvas = Image.new("RGB", (W, H), bg)
    cells = [(0, 0), (cell_w + gutter, 0), (0, cell_h + gutter), (cell_w + gutter, cell_h + gutter)]
    for im, (cx, cy) in zip(imgs, cells):
        oy = cy + (cell_h - im.size[1]) // 2      # 行内垂直居中,不裁切
        canvas.paste(im, (cx, oy))
    canvas.save(out_path, "JPEG", quality=quality, optimize=True)
    return out_path
