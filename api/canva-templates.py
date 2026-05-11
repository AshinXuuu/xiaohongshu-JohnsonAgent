"""
GET /api/canva-templates — 列出所有可用品牌模板

供前端"选模板"下拉框使用。
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import os
import sys
import json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cover.canva_client import get_access_token, list_brand_templates


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            access_token = get_access_token()
            templates = list_brand_templates(access_token)
        except Exception as e:
            return self._json(500, {"error": f"获取模板失败: {e}"})

        # 只返回前端需要的字段
        items = []
        for t in templates.get("items", []):
            items.append({
                "id": t.get("id"),
                "title": t.get("title"),
                "thumbnail": (t.get("thumbnail") or {}).get("url"),
            })
        self._json(200, {"templates": items})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
