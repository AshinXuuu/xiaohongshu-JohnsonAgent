"""KOS 出图引擎(固定 3:4 画布,填满裁切,不翻转、不变形)。

三类成品(均输出小红书竖图 3:4,默认 1080×1440):
  主图     —— 单张图裁成 3:4(cover 居中裁切)
  2合1横版 —— 两张横版图上下各占一半,每张填满裁成对应格,整体 3:4
  4合1竖版 —— 四张竖版图 2×2 田字,每张填满裁成对应格,整体 3:4

填充方式:cover —— 等比放大到铺满单元格,居中裁掉多余部分(不留白、不变形、不翻转)。
"""
from PIL import Image, ImageOps

# 小红书竖图 3:4
DEFAULT_CANVAS = (1080, 1440)
WHITE = (255, 255, 255)


def _open_rgb(path):
    # with 确保文件句柄随用随关(长驻多线程服务里此前会缓慢泄漏 fd)
    with Image.open(path) as img:
        # 手机照片常带 EXIF 方向标记;按标记物理旋正,避免重编码去掉 EXIF 后画面被转向
        img = ImageOps.exif_transpose(img)
        return img.convert("RGB")


def _fit_cover(img, cw, ch):
    """把图等比放大到铺满 cw×ch,再居中裁掉多余(不留白、不变形、不翻转)。"""
    iw, ih = img.size
    scale = max(cw / iw, ch / ih)
    nw, nh = max(cw, round(iw * scale)), max(ch, round(ih * scale))
    resized = img.resize((nw, nh), Image.LANCZOS)
    ox, oy = (nw - cw) // 2, (nh - ch) // 2
    return resized.crop((ox, oy, ox + cw, oy + ch))


def crop_cover(path, out_path, canvas=DEFAULT_CANVAS, quality=92):
    """主图:单张图裁成 3:4(cover 居中裁切)后输出。"""
    cw, ch = canvas
    _fit_cover(_open_rgb(path), cw, ch).save(out_path, "JPEG", quality=quality, optimize=True)
    return out_path


def stack_vertical(paths, out_path, canvas=DEFAULT_CANVAS, gutter=0, bg=WHITE, quality=92):
    """2合1:两张横版图上下各占一半,填满裁切,整体固定 3:4。"""
    if len(paths) != 2:
        raise ValueError(f"2合1 需要 2 张,收到 {len(paths)}")
    cw, ch = canvas
    cell_h = (ch - gutter) // 2
    base = Image.new("RGB", (cw, ch), bg)
    ys = [0, cell_h + gutter]
    for p, y in zip(paths, ys):
        base.paste(_fit_cover(_open_rgb(p), cw, cell_h), (0, y))
    base.save(out_path, "JPEG", quality=quality, optimize=True)
    return out_path


def grid_2x2(paths, out_path, canvas=DEFAULT_CANVAS, gutter=0, bg=WHITE, quality=92):
    """4合1:四张竖版图 2×2 田字,填满裁切,整体固定 3:4。"""
    if len(paths) != 4:
        raise ValueError(f"4合1 需要 4 张,收到 {len(paths)}")
    cw, ch = canvas
    cell_w = (cw - gutter) // 2
    cell_h = (ch - gutter) // 2
    base = Image.new("RGB", (cw, ch), bg)
    cells = [(0, 0), (cell_w + gutter, 0), (0, cell_h + gutter), (cell_w + gutter, cell_h + gutter)]
    for p, (cx, cy) in zip(paths, cells):
        base.paste(_fit_cover(_open_rgb(p), cell_w, cell_h), (cx, cy))
    base.save(out_path, "JPEG", quality=quality, optimize=True)
    return out_path
