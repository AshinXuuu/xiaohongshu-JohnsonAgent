"""
POST /api/login

请求体:
{
    "department": "市场部",
    "name": "徐昕",
    "emp_id": "888888"
}

响应(成功):
{
    "ok": true,
    "user": {"department":"市场部","name":"徐昕","emp_id":"888888","is_admin":true}
}

响应(失败):
{"error": "工号或姓名不匹配,请联系管理员"}

数据源:data/users.json(白名单,改人员只要改这个文件然后 push)
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import os
import sys
import json


ROOT = Path(__file__).resolve().parent.parent


def load_users():
    with (ROOT / "data" / "users.json").open("r", encoding="utf-8") as f:
        return json.load(f)


def find_user(department, name, emp_id):
    """白名单校验,匹配返回 user dict,不匹配返回 None"""
    data = load_users()
    name = (name or "").strip()
    emp_id = str(emp_id or "").strip()
    dept_users = data.get("users_by_dept", {}).get(department, [])
    for u in dept_users:
        # 工号字符串严格匹配
        if str(u.get("emp_id", "")).strip() == emp_id and u.get("name", "").strip() == name:
            return {
                "department": department,
                "name": name,
                "emp_id": emp_id,
                "is_admin": bool(u.get("is_admin")),
            }
    return None


def log_user_action(req_info: dict):
    """打 print 日志,Vercel Function Logs 可见,用于管理员追溯用量"""
    print(f"[LOGIN] {json.dumps(req_info, ensure_ascii=False)}", flush=True)


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        # GET 返回部门+人员清单(给前端下拉用),不含 is_admin 字段
        try:
            data = load_users()
            sanitized = {}
            for dept, lst in data.get("users_by_dept", {}).items():
                sanitized[dept] = [
                    {"name": u["name"]}  # 只返回姓名,不暴露工号
                    for u in lst
                ]
            self._json(200, {
                "departments": data.get("departments", []),
                "users_by_dept": sanitized,
            })
        except Exception as e:
            self._json(500, {"error": str(e)})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            req = json.loads(body)

            dept = (req.get("department") or "").strip()
            name = (req.get("name") or "").strip()
            emp_id = str(req.get("emp_id") or "").strip()

            if not all([dept, name, emp_id]):
                return self._json(400, {"error": "请填完整:部门 + 姓名 + 工号"})

            user = find_user(dept, name, emp_id)
            log_user_action({
                "action": "login_attempt",
                "department": dept,
                "name": name,
                "emp_id": emp_id,
                "success": user is not None,
            })

            if not user:
                return self._json(401, {
                    "error": "工号或姓名与所选部门不匹配,请核对后重试"
                })

            self._json(200, {"ok": True, "user": user})

        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
