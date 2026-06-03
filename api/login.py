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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.kv_store import log_event

ROOT = Path(__file__).resolve().parent.parent


def load_users():
    with (ROOT / "data" / "users.json").open("r", encoding="utf-8") as f:
        return json.load(f)


def find_user(department, name, emp_id, id_last6=None):
    """白名单校验,匹配返回 user dict,不匹配返回 (None, reason)。
    身份证后 6 位验证:
        - 若 users.json 里该用户已配置 id_last6 → 必须匹配
        - 若未配置 → 暂不校验(过渡期,允许 老员工没填的情况下继续登录)
    """
    data = load_users()
    name = (name or "").strip()
    emp_id = str(emp_id or "").strip()
    # 身份证末位可能是 X,统一大写比对(防止用户输小写 x)
    id_last6 = str(id_last6 or "").strip().upper()
    dept_users = data.get("users_by_dept", {}).get(department, [])
    for u in dept_users:
        if (str(u.get("emp_id", "")).strip() == emp_id
                and u.get("name", "").strip() == name):
            expect_id6 = str(u.get("id_last6", "")).strip().upper()
            if expect_id6:
                if expect_id6 != id_last6:
                    return None, '身份证后 6 位不正确'
            # 没配过则跳过校验,但要求请求里至少给了 6 位数字格式(前端会强制)
            return {
                "department": department,
                "name": name,
                "emp_id": emp_id,
                "is_admin": bool(u.get("is_admin")),
            }, None
    return None, '工号或姓名与所选部门不匹配'


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
            id_last6 = str(req.get("id_last6") or "").strip()

            if not all([dept, name, emp_id]):
                return self._json(400, {"error": "请填完整:部门 + 姓名 + 工号"})

            user, err = find_user(dept, name, emp_id, id_last6)
            log_user_action({
                "action": "login_attempt",
                "department": dept,
                "name": name,
                "emp_id": emp_id,
                "id6_provided": bool(id_last6),
                "success": user is not None,
                "fail_reason": err,
            })

            if not user:
                return self._json(401, {"error": err or "登录失败"})

            # 登录成功才记录到 KV(失败的不记)
            log_event('login', user)
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
