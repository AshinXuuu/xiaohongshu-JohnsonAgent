"""
GET /api/products
返回所有品牌和产品列表(给前端下拉框用)
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import json


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            import sys
            sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
            from lib.products_store import list_brands_products
            # 只返回名称结构,不带产品全文(节省流量);数据已收口到数据库,异常回退 JSON
            body = json.dumps({"brands": list_brands_products()}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._error(500, f"读取产品列表失败: {e}")

    def _error(self, code, msg):
        body = json.dumps({"error": msg}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
