"""GET /api/org → 返回当前组织的公开配置(品牌名、主色、启用模块、后台权限)。

前端的统一外壳(app.js)启动时拉一次,用来渲染顶栏/导航/换肤。
现在固定返回 johnson;未来可按子域名/请求头判断组织。
"""
import json
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent.parent


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        try:
            sys.path.insert(0, str(ROOT))
            from lib.org import public_org, DEFAULT_ORG
            qs = parse_qs(urlparse(self.path).query)
            org_id = (qs.get("org", [DEFAULT_ORG])[0]).strip() or DEFAULT_ORG
            self._json(200, public_org(org_id))
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
