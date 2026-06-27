"""POST /api/products-admin —— 后台产品管理(仅管理员)。

action:
  list              列出品牌(含 guidelines)+ 产品(含 id/content)
  add_product       新增产品 {brand, name, content}
  update_product    改产品 {id, name?, content?}
  delete_product    停用产品 {id}
  update_guidelines 改品牌资料 {brand, guidelines}
  reimport          从 products.json 强制重建(批量刷新,覆盖手工改动)

权限:一律服务端用 lib.auth 核对管理员,不信任前端。
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys
import json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.auth import is_admin
from lib import products_store


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

            if not is_admin(req.get("_user") or {}):
                return self._json(403, {"error": "无权访问,仅管理员可操作"})
            action = (req.get("action") or "").strip()

            if action == "list":
                return self._json(200, {"brands": products_store.get_all()})

            if action == "add_product":
                brand = (req.get("brand") or "").strip()
                name = (req.get("name") or "").strip()
                content = req.get("content") or ""
                if not (brand and name):
                    return self._json(400, {"error": "请填写品牌和产品名"})
                pid = products_store.add_product(brand, name, content)
                return self._json(200, {"ok": True, "id": pid})

            if action == "update_product":
                pid = req.get("id")
                if not pid:
                    return self._json(400, {"error": "缺少产品 id"})
                fields = {}
                if "name" in req:
                    fields["name"] = (req.get("name") or "").strip()
                if "content" in req:
                    fields["content"] = req.get("content") or ""
                if not fields:
                    return self._json(400, {"error": "没有要更新的字段"})
                products_store.update_product(pid, **fields)
                return self._json(200, {"ok": True})

            if action == "delete_product":
                pid = req.get("id")
                if not pid:
                    return self._json(400, {"error": "缺少产品 id"})
                products_store.deactivate_product(pid)
                return self._json(200, {"ok": True})

            if action == "update_guidelines":
                brand = (req.get("brand") or "").strip()
                if not brand:
                    return self._json(400, {"error": "缺少品牌"})
                products_store.update_guidelines(brand, req.get("guidelines") or "")
                return self._json(200, {"ok": True})

            if action == "reimport":
                res = products_store.reimport_from_json()
                return self._json(200, {"ok": True, **res})

            return self._json(400, {"error": "未知 action"})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
