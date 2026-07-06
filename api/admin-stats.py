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
from lib.auth import is_admin


ROOT = Path(__file__).resolve().parent.parent


# app → action 名单的映射
# 'all' 不过滤;'generate' 是文案+封面应用;'qa' 是问答应用
APP_ACTION_MAP = {
    'all':      None,  # 不过滤
    'generate': ['generate', 'generate_failed', 'cover_fields', 'cover_generate'],
    'qa':       ['qa', 'qa_failed'],
    'library':  ['download'],   # 产品资料库下载
}


def load_users():
    with (ROOT / "data" / "users.json").open("r", encoding="utf-8") as f:
        return json.load(f)


# is_admin 已统一移到 lib/auth.py(按 users.json 服务端核对角色,org_admin/super_admin 通过)


def enrich_user_data(stats: dict):
    """把 emp_id 翻译成"姓名(部门)",便于前端展示"""
    if not stats:
        return stats
    from lib.users_store import all_users
    id_to_info = {}
    for u in all_users():
        id_to_info[str(u.get("emp_id"))] = {
            "name": u.get("name", "未知"),
            "department": u.get("department", "未知"),
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
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            req = json.loads(body)
            from lib.session import user_from_headers
            user = user_from_headers(self.headers)
            if not user:
                return self._json(401, {"error": "未登录或登录已过期,请重新登录"})
            if not is_admin(user):
                return self._json(403, {"error": "无权访问,仅管理员可见"})

            if not _kv_available():
                return self._json(200, {
                    "kv_configured": False,
                    "message": "本地 SQLite 数据库初始化失败,请检查 data/ 目录权限。",
                })

            # 时间范围(天),<=0 全部
            try:
                days = int(req.get('days', 30))
            except Exception:
                days = 30

            app = (req.get('app') or 'all').strip().lower()

            # 平台总览:活跃人数 + 各模块用量
            if app == 'overview':
                from lib.kv_store import get_overview
                from lib.users_store import all_users
                ov = get_overview(days)
                info = {str(u.get('emp_id')): u for u in all_users()}
                ov['top_users'] = [{
                    'emp_id': x['key'],
                    'name': (info.get(x['key']) or {}).get('name', '未知'),
                    'department': (info.get(x['key']) or {}).get('department', ''),
                    'count': x['count'],
                } for x in ov.pop('top_users_raw', [])]
                try:
                    from lib.kos_store import kos_dashboard
                    ov['modules']['kos'] = kos_dashboard(days).get('published', 0)
                except Exception:
                    ov['modules']['kos'] = 0
                return self._json(200, {"app": "overview", "days": days, "kv_configured": True, "overview": ov})

            # KOS 是独立数据源(kos_packs/tasks),单独返回
            if app == 'kos':
                from lib.kos_store import kos_dashboard
                return self._json(200, {"app": "kos", "days": days, "kv_configured": True,
                                        "kos": kos_dashboard(days)})

            if app not in APP_ACTION_MAP:
                app = 'all'
            action_filter = APP_ACTION_MAP[app]

            stats = get_stats(action_filter=action_filter, days=days)
            stats = enrich_user_data(stats)
            stats['app'] = app
            stats['days'] = days

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
            import traceback; traceback.print_exc()
            print("[API-500] " + getattr(self, "path", "") + " " + repr(e), flush=True)
            self._json(500, {"error": "服务器开小差了,请稍后重试"})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
