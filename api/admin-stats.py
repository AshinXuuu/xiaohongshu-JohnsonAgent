"""POST /api/admin-stats

只能由管理员账号调用。

请求体:
{
    "_user": {"emp_id": "888888", "name": "徐昕", "department": "市场部"}
}

响应:
{
    "total": 1234,
    "by_user": [{"emp_id":"...","name":"...","department":"...","count":...}, ...],
    "by_dept": [...],
    "by_action": [...],
    "by_style": [...],
    "by_brand": [...],
    "by_daily": [{"key":"2026-05-12","count":50}, ...],
    "recent": [{"time":..., "action":..., "user":..., "details":...}, ...],
    "today": 30, "this_month": 200,
}
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import os
import sys
import json
import datetime

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.kv_store import get_stats, _kv_available


ROOT = Path(__file__).resolve().parent.parent


def load_users():
    with (ROOT / "data" / "users.json").open("r", encoding="utf-8") as f:
        return json.load(f)


def is_admin(user_info: dict) -> bool:
    """根据 users.json 验证是否为管理员账号"""
    if not user_info:
        return False
    emp_id = str(user_info.get("emp_id") or "").strip()
    name = (user_info.get("name") or "").strip()
    dept = (user_info.get("department") or "").strip()
    data = load_users()
    for u in data.get("users_by_dept", {}).get(dept, []):
        if (str(u.get("emp_id", "")).strip() == emp_id
                and u.get("name", "").strip() == name
                and u.get("is_admin")):
            return True
    return False


def enrich_user_data(stats: dict):
    """把 emp_id 翻译成"姓名(部门)",便于前端展示"""
    if not stats:
        return stats
    users_data = load_users()
    id_to_info = {}
    for dept, users in users_data.get("users_by_dept", {}).items():
        for u in users:
            id_to_info[str(u.get("emp_id"))] = {
                "name": u.get("name", "未知"),
                "department": dept,
            }
    enriched = []
    for item in stats.get("by_user_raw", []):
        info = id_to_info.get(item["key"], {"name": "未知", "department": "未知"})
        enriched.append({
            "emp_id": item["key"],
            "name": info["name"],
            "department": info["department"],
            "count": item["count"],
        })
    enriched.sort(key=lambda x: -x["count"])
    stats["by_user"] = enriched
    stats.pop("by_user_raw", None)
    return stats


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
            user = req.get("_user") or {}

            if not is_admin(user):
                return self._json(403, {"error": "无权访问,仅管理员可见"})

            if not _kv_available():
                return self._json(200, {
                    "kv_configured": False,
                    "message": "本地 SQLite 数据库初始化失败,请检查 data/ 目录权限。",
                })

            stats = get_stats()
            stats = enrich_user_data(stats)

            # 算今日/本月汇总
            today_key = datetime.datetime.now().strftime("%Y-%m-%d")
            this_month_prefix = datetime.datetime.now().strftime("%Y-%m")
            today_count = next((x["count"] for x in stats.get("by_daily", []) if x["key"] == today_key), 0)
            this_month_count = sum(x["count"] for x in stats.get("by_daily", []) if x["key"].startswith(this_month_prefix))

            stats["today"] = today_count
            stats["this_month"] = this_month_count
            stats["kv_configured"] = True

            self._json(200, stats)

        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
