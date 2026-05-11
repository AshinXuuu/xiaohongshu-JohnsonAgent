"""
POST /api/cover-generate

请求体:
{
    "photo_base64": "data:image/jpeg;base64,/9j/...",
    "main_title": "客厅多了它",
    "subtitle": "30+ 真香",
    "hua_text": "亲测✓"
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
SIZE = "768x1024"

MAX_PHOTO_BYTES = 4 * 1024 * 1024  # 4MB

# 3 个 prompt 变体,产出 3 张略有差异的图片
# 适配 Seedream(文生图)模型:描述整张封面长什么样,引用上传图作为风格参考
PROMPT_TEMPLATES = [
    # 变体 1:经典小红书爆款风(黄黑配色)
    "小红书爆款封面图,3:4 比例。参考上传的产品照片的真实场景和主体,生成一张同款风格的小红书封面:\n"
    "- 顶部大字标题「{main_title}」(粗黑体描边大字,黑色字+白色描边,占据画面上方 1/3)\n"
    "- 主标题下方小字副标题「{subtitle}」(白底黑边圆角胶囊小标签)\n"
    "- 右下角花字贴纸「{hua_text}」(黄色色块底+黑色粗字)\n"
    "整体风格:黄色背景或暖色调,粗体中文字干净有力,产品作为画面主体。所有文字必须清晰准确,一字不差。",
    # 变体 2:复古杂志感
    "小红书爆款封面 3:4 比例。基于上传产品图的内容,重绘一张复古杂志风格的封面:\n"
    "- 大标题「{main_title}」立体描边粗黑字,棕红色调,占据画面上方\n"
    "- 副标题「{subtitle}」深色小字,主标题正下方\n"
    "- 「{hua_text}」红色色块标签,放在画面右下角\n"
    "整体米色背景,有 NEW ARRIVAL 之类英文衬底,复古时尚感。中文字一字不差。",
    # 变体 3:清新干货风
    "小红书爆款封面 3:4 比例。基于上传产品图的内容,重绘一张清新干货风格的封面:\n"
    "- 顶部标题「{main_title}」黑色粗体大字,关键词用黄色高亮色块圈出\n"
    "- 副标题「{subtitle}」紧贴主标题下方\n"
    "- 「{hua_text}」白底黑边圆角胶囊,放在画面底部\n"
    "白色或浅色纸张质感背景,蓝色不规则手绘边框装饰。中文字清晰准确,严格按文字内容。",
]


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

            if not photo_b64:
                return self._json(400, {"error": "请上传产品照片"})
            if not main_title:
                return self._json(400, {"error": "主标题不能为空"})

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

            # 拼 3 个 prompt 并行调用
            prompts = [
                tpl.format(
                    main_title=main_title,
                    subtitle=subtitle or "(无副标题)",
                    hua_text=hua_text or "(无花字)",
                )
                for tpl in PROMPT_TEMPLATES
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
