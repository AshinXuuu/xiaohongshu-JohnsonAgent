"""POST /api/admin-events —— 分页 + 关键词搜索的操作明细(用量事件,仅管理员)。

请求:{ "_user":{...}, "app":"all|generate|qa|library", "days":30, "keyword":"", "page":0 }
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys
import json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.auth import is_admin
from lib.kv_store import get_events_page

APP_MAP = {
    'all':      None,
    'generate': ['generate', 'generate_failed', 'cover_fields', 'cover_generate'],
    'qa':       ['qa', 'qa_failed'],
    'library':  ['download'],
}


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length).decode("utf-8") if length else "{}")
            from lib.session import user_from_headers
            caller = user_from_headers(self.headers)
            if not caller:
                return self._json(401, {"error": "未登录或登录已过期,请重新登录"})
            if not is_admin(caller):
                return self._json(403, {"error": "无权访问,仅管理员可查看"})
            app = (req.get("app") or "all").strip().lower()
            action_filter = APP_MAP.get(app)  # 未知(如 kos/overview)按全部
            try:
                days = int(req.get("days", 30))
            except Exception:
                days = 30
            page = req.get("page", 0)
            try:
                page_size = int(req.get("page_size", 10))
            except Exception:
                page_size = 10
            res = get_events_page(action_filter, req.get("keyword", ""), days, page, page_size)
            return self._json(200, res)
        except Exception as e:
            import traceback; traceback.print_exc()
            print("[API-500] " + getattr(self, "path", "") + " " + repr(e), flush=True)
            self._json(500, {"error": "服务器开小差了,请稍后重试"})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
