"""
POST /api/canva-make-cover

请求体(JSON):
{
    "template_id": "EAFxxxxx",       // 从 /api/canva-templates 选
    "title": "客厅多了它",            // 主标题(从生成的 5 个候选里选一个)
    "subtitle": "30+ 真香",          // 副标题(可选)
    "tag": "居家健身",                // 角标(可选)
    "photo_base64": "data:image/jpeg;base64,/9j/..." // 业务上传的封面原图
    "photo_name": "tx5.jpg"          // 文件名(可选,只是 Canva 资产库展示用)
}

响应:
{
    "edit_url": "https://www.canva.com/design/xxx/edit",
    "design_id": "xxx"
}
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import os
import sys
import json
import base64
import re

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cover.canva_client import make_cover


# Vercel serverless 默认 body 上限约 4.5MB,base64 编码膨胀 33%,
# 所以原图建议 < 3MB(前端会做提示)
MAX_PHOTO_BYTES = 4 * 1024 * 1024  # 4MB


def parse_data_url(data_url):
    """解析 data URL(如 data:image/jpeg;base64,xxx)得到 (mime, bytes)"""
    m = re.match(r"data:([^;]+);base64,(.+)$", data_url.strip(), re.DOTALL)
    if not m:
        # 兼容纯 base64(不带 data: 前缀)
        return "image/jpeg", base64.b64decode(data_url)
    mime, b64 = m.group(1), m.group(2)
    return mime, base64.b64decode(b64)


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

            template_id = (req.get("template_id") or "").strip()
            title = (req.get("title") or "").strip()
            subtitle = (req.get("subtitle") or "").strip()
            tag = (req.get("tag") or "").strip()
            photo_b64 = req.get("photo_base64") or ""
            photo_name = (req.get("photo_name") or "cover.jpg").strip()

            if not template_id:
                return self._json(400, {"error": "缺少 template_id"})
            if not title:
                return self._json(400, {"error": "缺少 title"})
            if not photo_b64:
                return self._json(400, {"error": "请先上传产品照片"})

            try:
                mime, photo_bytes = parse_data_url(photo_b64)
            except Exception as e:
                return self._json(400, {"error": f"图片格式无法解析:{e}"})

            if len(photo_bytes) > MAX_PHOTO_BYTES:
                return self._json(413, {
                    "error": f"图片过大({len(photo_bytes)//1024//1024}MB),请压缩到 3MB 以内"
                })

            refresh = os.environ.get("CANVA_REFRESH_TOKEN", "")
            if not refresh:
                return self._json(503, {
                    "error": "Canva 集成尚未完成首次授权,请管理员访问 /api/canva-auth"
                })

            # 拼 autofill 文字数据(模板里有的字段才会被填)
            autofill_text = {"title": title}
            if subtitle:
                autofill_text["subtitle"] = subtitle
            if tag:
                autofill_text["tag"] = tag

            result = make_cover(
                refresh_token=refresh,
                template_id=template_id,
                autofill_text=autofill_text,
                photo_bytes=photo_bytes,
                photo_name=photo_name,
            )

            response = {
                "edit_url": result["edit_url"],
                "view_url": result["view_url"],
                "design_id": result["design_id"],
            }
            if result.get("new_refresh_token"):
                response["_admin_note"] = (
                    f"Canva 返回了新的 refresh_token,建议管理员更新 Vercel env: {result['new_refresh_token']}"
                )

            self._json(200, response)

        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
