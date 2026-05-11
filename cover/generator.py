"""
小红书封面生成器 — 4 套模板,纯 PIL 实现。

模板:
  T1 黄黑爆款 — 黄底 + 黑色描边粗字 + 白底黑边对勾胶囊
  T2 复古秋冬 — 米色背景 + 红棕立体描边字 + 英文大字背景
  T3 白底干货 — 白纸底 + 蓝色手绘边框 + 黑字黄高亮关键词
  T4 简洁产品 — 渐变背景 + 大字双色描边 + 产品图为主体

字体策略(中英混排):
  - CJK 字符 → Droid Sans Fallback Full (中文 fallback)
  - Latin / 数字 / 符号 → Poppins Bold
  - Emoji 用 PIL 自己画(✅ 画绿框白勾、👇 画箭头三角等)
"""
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

WIDTH, HEIGHT = 900, 1200

ROOT = Path(__file__).resolve().parent.parent

# 字体路径(项目内 assets 优先,系统字体兜底)
FONTS_CJK = [
    ROOT / "assets/fonts/AlibabaPuHuiTi-Heavy.ttf",
    ROOT / "assets/fonts/SourceHanSansSC-Heavy.otf",
    Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
    Path("/System/Library/Fonts/PingFang.ttc"),
]
FONTS_LATIN = [
    ROOT / "assets/fonts/AlibabaPuHuiTi-Heavy.ttf",  # 阿里普惠体也含 Latin
    Path("/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf"),
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
]


def _pick_font(candidates):
    for p in candidates:
        if p.exists():
            return str(p)
    raise RuntimeError(f"No font found in: {candidates}")


def font_cjk(size):
    return ImageFont.truetype(_pick_font(FONTS_CJK), size)


def font_latin(size):
    return ImageFont.truetype(_pick_font(FONTS_LATIN), size)


# ───────────── 中英混排渲染 ─────────────

def _is_cjk(ch):
    """判断字符是否需要用 CJK 字体渲染"""
    o = ord(ch)
    # CJK 统一表意 + 扩展A + 兼容表意 + CJK符号标点 + 全角符号
    return (
        0x4E00 <= o <= 0x9FFF
        or 0x3400 <= o <= 0x4DBF
        or 0xF900 <= o <= 0xFAFF
        or 0x3000 <= o <= 0x303F
        or 0xFF00 <= o <= 0xFFEF
    )


def _segment_text(text):
    """把字符串切成 [(text, is_cjk), ...] 连续段"""
    if not text:
        return []
    segs = []
    cur = text[0]
    cur_cjk = _is_cjk(text[0])
    for ch in text[1:]:
        is_cjk = _is_cjk(ch)
        # 空格归到相邻段(避免空格单独成段)
        if ch == " ":
            cur += ch
            continue
        if is_cjk == cur_cjk:
            cur += ch
        else:
            segs.append((cur, cur_cjk))
            cur = ch
            cur_cjk = is_cjk
    segs.append((cur, cur_cjk))
    return segs


def measure_mixed(draw, text, size):
    """测量中英混排文字的宽高"""
    segs = _segment_text(text)
    total_w = 0
    max_h = 0
    for s, is_cjk in segs:
        f = font_cjk(size) if is_cjk else font_latin(size)
        bbox = draw.textbbox((0, 0), s, font=f)
        total_w += bbox[2] - bbox[0]
        max_h = max(max_h, bbox[3] - bbox[1])
    return total_w, max_h


def draw_mixed(draw, pos, text, size, fill, stroke_fill=None, stroke_width=0):
    """中英混排绘制(支持描边)"""
    x, y = pos
    segs = _segment_text(text)
    for s, is_cjk in segs:
        f = font_cjk(size) if is_cjk else font_latin(size)
        bbox = draw.textbbox((0, 0), s, font=f)
        w = bbox[2] - bbox[0]
        if stroke_width > 0:
            draw.text((x, y), s, font=f, fill=fill,
                      stroke_width=stroke_width, stroke_fill=stroke_fill)
        else:
            draw.text((x, y), s, font=f, fill=fill)
        x += w


def draw_mixed_centered(draw, y, text, size, fill, **kwargs):
    """居中绘制,返回 (实际行高, x_start)"""
    w, h = measure_mixed(draw, text, size)
    x = (WIDTH - w) // 2
    draw_mixed(draw, (x, y), text, size, fill, **kwargs)
    return h, x


# ───────────── 几何图形(代替 emoji)─────────────

def draw_check(draw, center, size, fill_bg="#22C55E", fill_check="white"):
    """画一个绿底白勾的对勾图标"""
    cx, cy = center
    r = size // 2
    # 圆角方框背景
    draw.rounded_rectangle((cx - r, cy - r, cx + r, cy + r), radius=size // 5, fill=fill_bg)
    # 白色勾(简单 V 形)
    p1 = (cx - r * 0.45, cy)
    p2 = (cx - r * 0.1, cy + r * 0.35)
    p3 = (cx + r * 0.5, cy - r * 0.35)
    line_w = max(3, size // 10)
    draw.line([p1, p2, p3], fill=fill_check, width=line_w, joint="curve")


def draw_arrow_down(draw, top, size, fill="#1A1A1A"):
    """画向下三角箭头"""
    x, y = top
    half = size // 2
    draw.polygon([(x, y), (x + size, y), (x + half, y + size)], fill=fill)


def draw_star(draw, center, size, fill="#FFB300"):
    """画 5 角星"""
    import math
    cx, cy = center
    pts = []
    for i in range(10):
        angle = math.pi / 2 + i * math.pi / 5
        r = size if i % 2 == 0 else size * 0.4
        pts.append((cx + r * math.cos(angle), cy - r * math.sin(angle)))
    draw.polygon(pts, fill=fill)


def draw_capsule(draw, xy, fill, outline=None, outline_width=4):
    x1, y1, x2, y2 = xy
    radius = (y2 - y1) // 2
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=outline_width)


# ───────────── 图像处理 ─────────────

def fit_image_cover(img, box_w, box_h):
    src_ratio = img.width / img.height
    dst_ratio = box_w / box_h
    if src_ratio > dst_ratio:
        new_h, new_w = box_h, int(box_h * src_ratio)
    else:
        new_w, new_h = box_w, int(box_w / src_ratio)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - box_w) // 2
    top = (new_h - box_h) // 2
    return img.crop((left, top, left + box_w, top + box_h))


def round_corners(img, radius):
    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.width, img.height), radius=radius, fill=255)
    img.putalpha(mask)
    return img


# ───────────── 配色 ─────────────

YELLOW = "#FFD42D"
BLACK = "#1A1A1A"
CREAM = "#F5E8D5"
BROWN = "#6B3B1A"
RED = "#C8453E"
BLUE = "#1A6BCF"
HIGHLIGHT = "#FFD42D"


# ─────────────────── T1 黄黑爆款 ───────────────────

def render_t1(product_photo, main_title, main_title_2, sub_title, bullets, output_path):
    canvas = Image.new("RGB", (WIDTH, HEIGHT), YELLOW)
    draw = ImageDraw.Draw(canvas)

    # 主标题两行
    title_size = 96
    title_y = 70
    for line in [main_title, main_title_2]:
        if not line:
            continue
        w, h = measure_mixed(draw, line, title_size)
        x = (WIDTH - w) // 2
        draw_mixed(draw, (x, title_y), line, title_size, fill=BLACK,
                   stroke_fill="white", stroke_width=10)
        title_y += h + 12

    # 副标题胶囊(黑底黄字,旋转贴纸感)
    if sub_title:
        sub_size = 40
        w, h = measure_mixed(draw, sub_title, sub_size)
        pad_x, pad_y = 28, 14
        cap_w, cap_h = w + pad_x * 2, h + pad_y * 2
        cap = Image.new("RGBA", (cap_w + 30, cap_h + 30), (0, 0, 0, 0))
        cd = ImageDraw.Draw(cap)
        draw_capsule(cd, (15, 15, 15 + cap_w, 15 + cap_h), fill=BLACK)
        # 用 draw_mixed 在小图层上
        draw_mixed(cd, (15 + pad_x, 15 + pad_y - 5), sub_title, sub_size, fill=YELLOW)
        cap = cap.rotate(-4, resample=Image.BICUBIC, expand=True)
        canvas.paste(cap, ((WIDTH - cap.width) // 2 + 100, title_y - 10), cap)

    # 产品图
    photo_top = title_y + 90
    photo_h = 580
    photo_w = WIDTH - 80
    photo = Image.open(product_photo)
    photo = fit_image_cover(photo, photo_w, photo_h)
    photo = round_corners(photo, 32)
    canvas.paste(photo, (40, photo_top), photo)

    # 向下箭头装饰
    draw_arrow_down(draw, (WIDTH - 120, photo_top - 50), 50, fill=BLACK)

    # 底部对勾胶囊列表
    list_top = photo_top + photo_h + 30
    if bullets:
        bul_size = 38
        for i, item in enumerate(bullets[:3]):
            w, h = measure_mixed(draw, item, bul_size)
            cap_h = h + 26
            cap_w = w + 110
            x = (WIDTH - cap_w) // 2
            y = list_top + i * (cap_h + 16)
            draw_capsule(draw, (x, y, x + cap_w, y + cap_h),
                         fill="white", outline=BLACK, outline_width=5)
            draw_check(draw, (x + 50, y + cap_h // 2), 40)
            draw_mixed(draw, (x + 90, y + 12), item, bul_size, fill=BLACK)

    canvas.save(output_path, "PNG", optimize=True)
    return output_path


# ─────────────────── T2 复古秋冬 ───────────────────

def render_t2(product_photo, main_title, main_title_2, sub_title, bullets, output_path):
    canvas = Image.new("RGB", (WIDTH, HEIGHT), CREAM)
    draw = ImageDraw.Draw(canvas)

    # 顶部英文大字背景
    en_font = font_latin(110)
    draw.text((30, 70), "NEW ARRIVAL", font=en_font, fill="#E8D9BC")

    # 顶部"特惠/上新"小色块
    if sub_title:
        sub_size = 34
        w, h = measure_mixed(draw, sub_title, sub_size)
        pad = 22
        cap_w, cap_h = w + pad * 2, h + 18
        x = (WIDTH - cap_w) // 2
        draw_capsule(draw, (x, 200, x + cap_w, 200 + cap_h), fill=BROWN)
        draw_mixed(draw, (x + pad, 200 + 5), sub_title, sub_size, fill=CREAM)

    # 主标题(红棕色立体描边大字)
    title_size = 110
    title_y = 280
    for line in [main_title, main_title_2]:
        if not line:
            continue
        w, h = measure_mixed(draw, line, title_size)
        x = (WIDTH - w) // 2
        # 立体阴影(后绘制偏移版本作为阴影)
        draw_mixed(draw, (x + 6, title_y + 8), line, title_size, fill=BROWN)
        # 主字 + 描边
        draw_mixed(draw, (x, title_y), line, title_size, fill=CREAM,
                   stroke_fill=BROWN, stroke_width=9)
        title_y += h + 8

    # 产品图
    photo_top = title_y + 60
    photo_h = 480
    photo_w = WIDTH - 120
    photo = Image.open(product_photo)
    photo = fit_image_cover(photo, photo_w, photo_h)
    photo = round_corners(photo, 16)
    canvas.paste(photo, (60, photo_top), photo)

    # 右下角红色色块
    red_text_1 = bullets[0] if bullets else "限时上新"
    red_text_2 = bullets[1] if len(bullets) > 1 else "立即查看"
    rb_w, rb_h = 280, 120
    rb_x = WIDTH - rb_w - 40
    rb_y = photo_top + photo_h - 80
    draw.rectangle((rb_x, rb_y, rb_x + rb_w, rb_y + rb_h), fill=RED)
    for i, t in enumerate([red_text_1, red_text_2]):
        w, _ = measure_mixed(draw, t, 48)
        draw_mixed(draw, (rb_x + (rb_w - w) // 2, rb_y + 10 + i * 50), t, 48, fill="white")

    canvas.save(output_path, "PNG", optimize=True)
    return output_path


# ─────────────────── T3 白底干货 ───────────────────

def render_t3(product_photo, main_title, main_title_2, sub_title, bullets, output_path):
    canvas = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(canvas)

    # 不规则蓝色手绘边框
    border_pts = [
        (40, 50), (300, 35), (600, 55), (860, 40),
        (875, 320), (855, 680), (870, 950), (855, 1170),
        (560, 1185), (260, 1165), (35, 1180),
        (50, 880), (30, 600), (45, 280),
    ]
    draw.line(border_pts + [border_pts[0]], fill=BLUE, width=8, joint="curve")

    # 顶部 # 话题(用 Latin 字体里的 # 应该 OK)
    if sub_title:
        tag_size = 56
        full_tag = f"#{sub_title}"
        w, h = measure_mixed(draw, full_tag, tag_size)
        draw_mixed(draw, ((WIDTH - w) // 2, 110), full_tag, tag_size, fill=BLACK)

    # 主标题(部分行加黄高亮)
    t_size = 94
    title_y = 210
    for idx, line in enumerate([main_title, main_title_2]):
        if not line:
            continue
        w, h = measure_mixed(draw, line, t_size)
        x = (WIDTH - w) // 2
        # 第二行加黄底高亮
        if idx == 1:
            hl = Image.new("RGBA", (w + 24, h + 16), (255, 212, 45, 200))
            canvas.paste(hl, (x - 12, title_y + 4), hl)
        draw_mixed(draw, (x, title_y), line, t_size, fill=BLACK)
        title_y += h + 18

    # 产品图
    photo_top = title_y + 40
    photo_h = 520
    photo_w = WIDTH - 120
    photo = Image.open(product_photo)
    photo = fit_image_cover(photo, photo_w, photo_h)
    photo = round_corners(photo, 24)
    canvas.paste(photo, (60, photo_top), photo)

    # 底部黄色胶囊
    if bullets:
        cap_size = 36
        list_top = photo_top + photo_h + 20
        for i, item in enumerate(bullets[:2]):
            w, h = measure_mixed(draw, item, cap_size)
            pad = 28
            cap_w, cap_h = w + pad * 2, h + 22
            x = (WIDTH - cap_w) // 2
            y = list_top + i * (cap_h + 10)
            draw_capsule(draw, (x, y, x + cap_w, y + cap_h),
                         fill=HIGHLIGHT, outline=BLACK, outline_width=4)
            draw_mixed(draw, (x + pad, y + 10), item, cap_size, fill=BLACK)

    canvas.save(output_path, "PNG", optimize=True)
    return output_path


# ─────────────────── T4 简洁产品 ───────────────────

def render_t4(product_photo, main_title, main_title_2, sub_title, bullets, output_path):
    # 渐变背景
    canvas = Image.new("RGB", (WIDTH, HEIGHT), "white")
    d_grad = ImageDraw.Draw(canvas)
    for y in range(HEIGHT):
        ratio = y / HEIGHT
        r = int(255 * (1 - ratio * 0.05))
        g = int(255 * (1 - ratio * 0.05))
        b = int(255 * (1 - ratio * 0.15))
        d_grad.line([(0, y), (WIDTH, y)], fill=(r, g, b))
    draw = ImageDraw.Draw(canvas)

    # 主标题(黑字带黄色阴影立体)
    t_size = 104
    title_y = 110
    for line in [main_title, main_title_2]:
        if not line:
            continue
        w, h = measure_mixed(draw, line, t_size)
        x = (WIDTH - w) // 2
        # 黄色阴影 offset
        draw_mixed(draw, (x + 8, title_y + 10), line, t_size, fill=YELLOW)
        # 黑色主字 + 白描边
        draw_mixed(draw, (x, title_y), line, t_size, fill=BLACK,
                   stroke_fill="white", stroke_width=6)
        title_y += h + 14

    # 副标题
    if sub_title:
        sub_size = 40
        w, _ = measure_mixed(draw, sub_title, sub_size)
        draw_mixed(draw, ((WIDTH - w) // 2, title_y + 10), sub_title, sub_size, fill="#666")
        title_y += 60

    # 产品图占主体
    photo_top = title_y + 50
    photo_h = HEIGHT - photo_top - 40
    photo_w = WIDTH - 80
    photo = Image.open(product_photo)
    photo = fit_image_cover(photo, photo_w, photo_h)
    photo = round_corners(photo, 32)
    canvas.paste(photo, (40, photo_top), photo)

    # 右上角小星星装饰
    draw_star(draw, (WIDTH - 80, 60), 35, fill="#FFB300")

    canvas.save(output_path, "PNG", optimize=True)
    return output_path


# ─────────────────── 统一入口 ───────────────────

TEMPLATES = {
    "T1": render_t1,
    "T2": render_t2,
    "T3": render_t3,
    "T4": render_t4,
}

TEMPLATE_INFO = {
    "T1": ("黄黑爆款", "种草/干货/避雷类"),
    "T2": ("复古秋冬", "促销/上新类"),
    "T3": ("白底干货", "教程/科普类"),
    "T4": ("简洁产品", "通用,产品为主"),
}


def render(template, product_photo, main_title, main_title_2="", sub_title="",
           bullets=None, output_path="cover.png"):
    if template not in TEMPLATES:
        raise ValueError(f"未知模板: {template}。可选: {list(TEMPLATES)}")
    bullets = bullets or []
    return TEMPLATES[template](
        product_photo=product_photo,
        main_title=main_title,
        main_title_2=main_title_2,
        sub_title=sub_title,
        bullets=bullets,
        output_path=output_path,
    )
