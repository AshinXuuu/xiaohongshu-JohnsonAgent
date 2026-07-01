"""POST /api/audit-log —— 查询管理员操作审计日志(仅管理员,近30天)。

请求:{ "_user": {...}, "category": "全部|用户|产品|任务" }
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys
import json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.auth import is_admin
from lib import audit


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
            req = json.loads(self.rfile.read(length).decode("utf-8") if length else "{}")
            if not is_admin(req.get("_user") or {}):
                return self._json(403, {"error": "无权访问,仅管理员可查看"})
            category = (req.get("category") or "全部").strip()
            logs = audit.recent(days=30, category=category)
            return self._json(200, {"logs": logs, "days": 30})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
