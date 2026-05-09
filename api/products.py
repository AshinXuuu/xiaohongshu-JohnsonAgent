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
            # data/products.json 位于项目根目录
            data_path = Path(__file__).resolve().parent.parent / "data" / "products.json"
            with data_path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            # 只返回名称结构,不带产品全文(节省流量)
            brands = []
            for b in data.get("brands", []):
                brands.append({
                    "name": b["name"],
                    "products": [{"name": p["name"]} for p in b.get("products", [])]
                })

            body = json.dumps({"brands": brands}, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self._error(500, "products.json 还未生成,请先运行 python scripts/build_products.py")
        except Exception as e:
            self._error(500, f"读取产品列表失败: {e}")

    def _error(self, code, msg):
        body = json.dumps({"error": msg}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
