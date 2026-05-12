"""
POST /api/cover-generate

请求体:
{
    "photo_base64": "data:image/jpeg;base64,/9j/...",
    "main_title": "客厅多了它",
    "subtitle": "30+ 真香",
    "hua_text": "亲测好用 三个月不踩坑",   // 支持 20 字以内,可多行
    "style": "种草氛围"                       // 可选,4 选 1
}

后端做的事:
  1. 解析 base64 图片
  2. 并行调用豆包 SeedEdit 3.0 三次,每次产出 1 张图
  3. 每次的 prompt 略有差异(让 3 张图风格略错位),提高业务"挑得到一张"的概率
  4. 返回 3 个图片 URL(豆包 API 直接返回 CDN URL)

响应:
{
    "images": ["https://...png", "https://...png", "https://...png"],
    "errors": []   // 若部分失败,这里列出错误信息
}

注意:
  - SeedEdit 输入图片支持 base64 或 URL
  - watermark: false 让输出无水印
  - size: "768x1024" 是 3:4
"""
from http.server import BaseHTTPRequestHandler
import os
import json
import base64
import re
import urllib.request
import urllib.error
import threading


DOUBAO_URL = "https://ark.cn-beijing.volces.com/api/v3/images/generations"

# 模型 ID 从环境变量读取,方便切换不同版本/endpoint
# 默认 Seedream 5.0 lite(文生图,参考上传图风格但会重新创作画面)
DOUBAO_MODEL = os.environ.get("DOUBAO_MODEL", "doubao-seedream-5-0-lite-260128")

# 3:4 比例(小红书封面)
# Seedream 5.0 lite 要求 ≥ 3,686,400 像素(368.64 万);1920×2560 = 4,915,200,刚好覆盖
# 1920/2560 = 3/4 = 小红书封面标准比例
SIZE = os.environ.get("DOUBAO_IMAGE_SIZE", "1920x2560")

MAX_PHOTO_BYTES = 4 * 1024 * 1024  # 4MB

# 通用文字渲染要求(所有风格都拼在末尾,强化中文文字稳定性)
TEXT_REQUIREMENTS = (
    "\n\n【文字渲染要求 — 极其重要】\n"
    "1. 中文文字必须**清晰可读、字形完整、一字不差**,严格按上述提供的文字内容渲染,不要改一个字\n"
    "2. 主标题字号最大,副标题中等,花字小贴纸\n"
    "3. 文字与产品/场景不重叠,有清晰图层关系\n"
    "4. 字体要求:粗体中文 sans-serif,有白色描边或黑色阴影增强可读性\n"
    "5. 如果花字内容较长(>10 字),允许自然换行成 2-3 行,排版灵活、活泼但整齐"
)

# 4 套风格,每套 3 个 prompt 变体,产出 3 张略有差异的图片
STYLE_PROMPTS = {
    "种草氛围": [
        # 变体 1:真人氛围生活感
        "小红书爆款封面,3:4 比例,种草测评类。参考上传产品图的真实场景,重绘一张「真人居家生活感」封面:\n"
        "- 暖色调粉黄色光,柔和阳光照射,有家居生活感(沙发、绿植、阳光感)\n"
        "- 产品作为画面右侧或中部主体,左侧有「模特正在使用」的人物剪影或手部\n"
        "- 顶部主标题:「{main_title}」(粗黑体大字 + 白色描边 + 黑色细阴影,占据上方 1/3)\n"
        "- 主标题旁:「{subtitle}」(黑底黄字圆角胶囊,有微微旋转的贴纸感)\n"
        "- 右下角花字:「{hua_text}」(黄色色块底 + 手写感粗黑字)\n"
        "整体氛围:30+ 都市女性的精致居家生活,高级、真实、有质感。\n"
        "审美参考:小红书「生活方式」类爆款封面、高 CTR(点击率)。\n"
        "技法关键:warm color grading, soft light, lifestyle photography aesthetic, vibrant Chinese typography, sticker-style elements.",
        # 变体 2:产品近景特写
        "小红书爆款封面,3:4 比例,种草氛围。基于上传产品图,重绘一张「产品特写+柔光感」封面:\n"
        "- 产品近景特写占据画面 60%,有柔和的光晕和景深虚化\n"
        "- 背景米黄色或淡粉色,带细微 grain 颗粒质感\n"
        "- 顶部主标题:「{main_title}」(超大粗黑字 + 白色厚描边)\n"
        "- 主标题下面副标题:「{subtitle}」(纯黑细字,小标签状)\n"
        "- 右上角花字:「{hua_text}」(亮黄色不规则贴纸,微旋转)\n"
        "整体:广告大片质感,色调高级,文字层次清晰。\n"
        "技法:cinematic product shot, beautiful bokeh, magazine-quality typography, bold Chinese characters with white outline.",
        # 变体 3:多元素拼贴感
        "小红书爆款封面,3:4 比例,生活方式拼贴风。基于上传产品图重绘:\n"
        "- 画面中部产品 + 旁边散落几个生活小物(咖啡杯、瑜伽垫、植物、运动鞋等)\n"
        "- 整体米色或浅黄背景,有手撕纸/拍立得边框装饰\n"
        "- 顶部主标题:「{main_title}」(双色立体描边大字,黑底+黄阴影)\n"
        "- 「{subtitle}」紧贴主标题下方,白底黑字小贴纸\n"
        "- 「{hua_text}」散落在右下,可以是手写感的多行小字\n"
        "技法:lifestyle collage layout, polaroid frames, vibrant typography stickers, warm color palette.",
    ],

    "干货教程": [
        # 变体 1:白底教程经典
        "小红书爆款封面,3:4 比例,干货教程类。基于上传产品图重绘一张「白底干货笔记」封面:\n"
        "- 白色或浅米色纸张质感背景,有微妙 grain texture\n"
        "- 蓝色不规则手绘边框(像马克笔涂的)环绕整个画面\n"
        "- 顶部 # 话题标签:#使用教程\n"
        "- 主标题:「{main_title}」(超大粗黑字,关键词用黄色色块高亮圈出)\n"
        "- 副标题:「{subtitle}」(黑色细字,主标题正下方)\n"
        "- 中部产品图清晰,白底无干扰\n"
        "- 底部花字:「{hua_text}」(白底黑边圆角胶囊,可多行)\n"
        "技法:notebook aesthetic, blue hand-drawn borders, bold yellow highlights, magazine-style typography.",
        # 变体 2:答疑科普感
        "小红书爆款封面,3:4 比例,科普答疑风。基于上传产品图重绘:\n"
        "- 浅灰白渐变背景,有方框/网格细节\n"
        "- 主标题:「{main_title}」(超大粗黑字 + 黑色阴影,某些关键词带黄色下划线)\n"
        "- 主标题下方副标题:「{subtitle}」(深灰色,字号较小)\n"
        "- 中部产品图,有指示箭头/标注线\n"
        "- 底部 3 个白底黑边对勾(✓)胶囊列表,其中之一显示:「{hua_text}」\n"
        "技法:infographic style, clean grid layout, bold question marks, bold answers, didactic typography.",
        # 变体 3:避雷红黑警示
        "小红书爆款封面,3:4 比例,避雷干货风。基于上传产品图重绘:\n"
        "- 米黄色背景,有红色感叹号「!」警示标记元素\n"
        "- 顶部主标题:「{main_title}」(超大粗黑字 + 红色描边或下划线)\n"
        "- 副标题:「{subtitle}」(红底白字横条强调)\n"
        "- 产品图清晰居中,旁边有圈圈/箭头标注重点\n"
        "- 底部花字:「{hua_text}」(黄色色块底 + 黑色粗字)\n"
        "技法:warning poster style, red accents, attention-grabbing typography, doodle annotations.",
    ],

    "促销爆款": [
        # 变体 1:大促爆款红黄
        "小红书爆款封面,3:4 比例,促销活动类。基于上传产品图重绘一张「大促爆款」封面:\n"
        "- 红黄高饱和色块拼贴背景,有「上新」「特惠」英文衬底\n"
        "- 顶部超大主标题:「{main_title}」(黄字 + 红色厚描边 + 黑色斜阴影,极强视觉冲击)\n"
        "- 主标题下方副标题:「{subtitle}」(白底红字胶囊)\n"
        "- 中部产品图,有「爆」字标签贴在角落\n"
        "- 右下花字:「{hua_text}」(可多行,红黄相间的促销贴纸感)\n"
        "技法:e-commerce sale poster, high contrast red/yellow palette, urgent typography, sale stickers, energetic composition.",
        # 变体 2:限时倒计时
        "小红书爆款封面,3:4 比例,限时活动。基于上传产品图重绘:\n"
        "- 深红或暗紫渐变背景,中心有放射光线效果\n"
        "- 顶部「限时」或「今晚 8 点」小标签(白底红字)\n"
        "- 主标题:「{main_title}」(金色粗黑大字 + 黑色阴影,有奖杯/皇冠装饰)\n"
        "- 副标题:「{subtitle}」(白色小字,主标题下方)\n"
        "- 产品图居中突出\n"
        "- 底部花字:「{hua_text}」(亮金色胶囊标签,可多行堆叠)\n"
        "技法:premium sale aesthetic, gold accents, radial light burst, luxury sale typography.",
        # 变体 3:双 11 风格
        "小红书爆款封面,3:4 比例,双 11 / 大促节点风。基于上传产品图重绘:\n"
        "- 黑红配色,带 NEW / HOT / SALE 等英文衬底大字\n"
        "- 顶部主标题:「{main_title}」(立体描边白字 + 红色阴影 + 黄色高亮关键词)\n"
        "- 副标题:「{subtitle}」(白色细字)\n"
        "- 产品图被红色色块/圆形框衬托\n"
        "- 右下角「价格爆炸」风格贴纸显示:「{hua_text}」\n"
        "技法:Singles Day campaign style, dramatic typography, explosion stickers, retail urgency.",
    ],

    "运动潮感": [
        # 变体 1:健身房力量感
        "小红书爆款封面,3:4 比例,运动健身潮感。基于上传产品图重绘:\n"
        "- 深色背景(黑色 / 深灰)+ 高对比黄色或荧光绿点缀\n"
        "- 有动感线条 / 速度感模糊 / 运动汗水元素\n"
        "- 顶部主标题:「{main_title}」(超大粗黑字 + 荧光黄描边 + 黑色阴影,字体有运动力量感)\n"
        "- 副标题:「{subtitle}」(白底黑字小标签,微倾斜)\n"
        "- 产品居中,有「#力量」或「#燃脂」角标\n"
        "- 底部花字:「{hua_text}」(荧光黄底 + 黑色粗字胶囊,可多行)\n"
        "技法:athletic poster, neon yellow accents, dynamic motion lines, gym aesthetic, bold sport typography, Nike/Adidas-inspired layout.",
        # 变体 2:户外阳光潮酷
        "小红书爆款封面,3:4 比例,户外运动潮。基于上传产品图重绘:\n"
        "- 蓝天/阳光感渐变背景,有镜头光晕 lens flare\n"
        "- 主标题:「{main_title}」(超大白字 + 黑色厚描边 + 蓝色阴影)\n"
        "- 副标题:「{subtitle}」(黄色斜体小字,主标题旁)\n"
        "- 产品有阳光照射感,色彩鲜艳\n"
        "- 花字:「{hua_text}」(白底黑字 + 黄色波浪下划线,可多行)\n"
        "技法:outdoor sport aesthetic, sunny vibes, sport magazine cover style, dynamic angles.",
        # 变体 3:暗黑燃脂硬核
        "小红书爆款封面,3:4 比例,硬核燃脂风。基于上传产品图重绘:\n"
        "- 黑色或暗红渐变背景,有铁锈 / 金属质感\n"
        "- 顶部 BURN / POWER / HIIT 英文衬底大字\n"
        "- 主标题:「{main_title}」(白字 + 红色厚描边 + 黑色阴影,字体粗壮硬朗)\n"
        "- 副标题:「{subtitle}」(红色小字加粗)\n"
        "- 产品有金属反光感、汗水滴落\n"
        "- 花字:「{hua_text}」(红黑双色立体描边,可不规则多行排列)\n"
        "技法:hardcore gym poster, dark mood, metallic textures, aggressive typography, fitness intensity.",
    ],
}

DEFAULT_STYLE = "种草氛围"


def parse_data_url(data_url):
    """解析 data URL 返回 (mime, bytes)"""
    m = re.match(r"data:([^;]+);base64,(.+)$", data_url.strip(), re.DOTALL)
    if not m:
        return "image/jpeg", base64.b64decode(data_url)
    return m.group(1), base64.b64decode(m.group(2))


def call_seededit(prompt, photo_base64_clean, mime, results, idx):
    """同步调用一次豆包 SeedEdit,把结果写到 results[idx]"""
    api_key = os.environ.get("DOUBAO_API_KEY", "").strip()
    if not api_key:
        results[idx] = {"error": "DOUBAO_API_KEY 未配置"}
        return

    # SeedEdit 接受 data URL 形式的 image
    image_data_url = f"data:{mime};base64,{photo_base64_clean}"

    payload = {
        "model": DOUBAO_MODEL,
        "prompt": prompt,
        "image": image_data_url,
        "size": SIZE,
        "response_format": "url",
        "watermark": False,
    }

    req = urllib.request.Request(
        DOUBAO_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=55) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            items = data.get("data", [])
            if items and items[0].get("url"):
                results[idx] = {"url": items[0]["url"]}
            else:
                results[idx] = {"error": f"豆包返回结构异常: {data}"}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore") if e.fp else ""
        results[idx] = {"error": f"豆包 API 错误 {e.code}: {detail[:300]}"}
    except Exception as e:
        results[idx] = {"error": f"调用异常: {e}"}


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            req = json.loads(body)

            photo_b64 = req.get("photo_base64") or ""
            main_title = (req.get("main_title") or "").strip()
            subtitle = (req.get("subtitle") or "").strip()
            hua_text = (req.get("hua_text") or "").strip()
            style = (req.get("style") or "").strip() or DEFAULT_STYLE

            if not photo_b64:
                return self._json(400, {"error": "请上传产品照片"})
            if not main_title:
                return self._json(400, {"error": "主标题不能为空"})
            if style not in STYLE_PROMPTS:
                return self._json(400, {"error": f"未知风格:{style},可选 {list(STYLE_PROMPTS)}"})

            try:
                mime, photo_bytes = parse_data_url(photo_b64)
            except Exception as e:
                return self._json(400, {"error": f"图片格式无法解析:{e}"})

            if len(photo_bytes) > MAX_PHOTO_BYTES:
                return self._json(413, {
                    "error": f"图片过大({len(photo_bytes)//1024//1024}MB),请压缩到 3MB 内"
                })

            # 提取 clean base64(去 data URL 前缀)
            m = re.match(r"data:[^;]+;base64,(.+)$", photo_b64.strip(), re.DOTALL)
            photo_base64_clean = m.group(1) if m else photo_b64.strip()

            # 拼 3 个 prompt(同一风格的 3 个变体)并行调用
            templates = STYLE_PROMPTS[style]
            prompts = [
                (tpl + TEXT_REQUIREMENTS).format(
                    main_title=main_title,
                    subtitle=subtitle or "(无副标题)",
                    hua_text=hua_text or "(无花字)",
                )
                for tpl in templates
            ]

            results = [None, None, None]
            threads = []
            for i, p in enumerate(prompts):
                t = threading.Thread(
                    target=call_seededit,
                    args=(p, photo_base64_clean, mime, results, i),
                )
                t.start()
                threads.append(t)
            for t in threads:
                t.join(timeout=58)

            images = [r["url"] for r in results if r and "url" in r]
            errors = [r["error"] for r in results if r and "error" in r]

            if not images:
                return self._json(502, {
                    "error": "3 张都生成失败",
                    "details": errors,
                })

            self._json(200, {"images": images, "errors": errors})

        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
