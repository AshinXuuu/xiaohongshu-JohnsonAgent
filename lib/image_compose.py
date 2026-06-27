"""KOS 防查重出图引擎。

把素材库里的「可拼图」素材随机裁切后拼成成品图,用于矩阵号发布时规避平台判重:
  - 随机裁切:每张源图按 cover 方式填充单元格 + 随机轻微缩放 + 随机偏移(不翻转)
  - 随机版式:2合1 随机左右 / 上下;4合1 为 2×2
  - 重编码:输出 JPEG(数字指纹改变,肉眼无差异)
  - 清 EXIF:输出不带任何拍摄元信息(Pillow 默认不写,且这里全新画布合成)

同一组源图 + 不同随机种子 → 产出视觉相近但字节/裁切各不相同的成品。
"""
import random
from PIL import Image

# 小红书竖图 3:4(常见首图比例)
DEFAULT_CANVAS = (1080, 1440)


def _open_rgb(path):
    img = Image.open(path)
    img = img.convert("RGB")        # 去掉 alpha / 调色板,统一 RGB
    return img


def _cover_crop(img, tw, th, rng, zoom_max=1.12):
    """把 img 以 cover 方式填满 tw×th:保比缩放到能覆盖,叠加随机轻缩放,再随机偏移裁切。
    不翻转、不拉伸变形。"""
    iw, ih = img.size
    base_scale = max(tw / iw, th / ih)
    scale = base_scale * (1.0 + rng.uniform(0.0, zoom_max - 1.0))
    nw, nh = max(tw, int(iw * scale + 0.5)), max(th, int(ih * scale + 0.5))
    resized = img.resize((nw, nh), Image.LANCZOS)
    ox = rng.randint(0, nw - tw)
    oy = rng.randint(0, nh - th)
    return resized.crop((ox, oy, ox + tw, oy + th))


def _cells_4(cw, ch, g):
    """2×2 单元格坐标(含间隙 g)。"""
    w = (cw - g) // 2
    h = (ch - g) // 2
    return [
        (0, 0, w, h),
        (cw - w, 0, w, h),
        (0, ch - h, w, h),
        (cw - w, ch - h, w, h),
    ]


def compose(paths, out_path, canvas=DEFAULT_CANVAS, seed=None, gutter=0, quality=88):
    """把 paths 里的图拼成一张成品输出到 out_path。
    len(paths)==1 单图;==2 两拼一;==4 四拼一。
    seed 固定则可复现(测试用);生产用随机种子保证每次不同。
    """
    rng = random.Random(seed)
    cw, ch = canvas
    g = gutter
    base = Image.new("RGB", (cw, ch), (255, 255, 255))
    imgs = [_open_rgb(p) for p in paths]
    n = len(imgs)

    if n == 1:
        base.paste(_cover_crop(imgs[0], cw, ch, rng), (0, 0))
    elif n == 2:
        if rng.random() < 0.5:
            # 左右
            lw = (cw - g) // 2
            rw = cw - lw - g
            base.paste(_cover_crop(imgs[0], lw, ch, rng), (0, 0))
            base.paste(_cover_crop(imgs[1], rw, ch, rng), (lw + g, 0))
        else:
            # 上下
            th = (ch - g) // 2
            bh = ch - th - g
            base.paste(_cover_crop(imgs[0], cw, th, rng), (0, 0))
            base.paste(_cover_crop(imgs[1], cw, bh, rng), (0, th + g))
    elif n == 4:
        for im, (x, y, w, h) in zip(imgs, _cells_4(cw, ch, g)):
            base.paste(_cover_crop(im, w, h, rng), (x, y))
    else:
        raise ValueError(f"只支持 1 / 2 / 4 张拼接,收到 {n} 张")

    # 保存为全新 JPEG:重编码 + 不带 EXIF
    base.save(out_path, "JPEG", quality=quality, optimize=True)
    return out_path
