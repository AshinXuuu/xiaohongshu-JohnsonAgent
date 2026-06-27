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
    数据源已收口到数据库(lib/users_store),DB 异常时自动回退 users.json。
    身份证后 6 位验证:
        - 该用户已配置 id_last6 → 必须匹配
        - 未配置 → 暂不校验(过渡期)
    """
    from lib.users_store import get_user
    id6 = str(id_last6 or "").strip().upper()  # 末位 X 统一大写
    u = get_user(department, name, emp_id)
    if not u:
        return None, '工号或姓名与所选部门不匹配'
    expect_id6 = str(u.get("id_last6") or "").strip().upper()
    if expect_id6 and expect_id6 != id6:
        return None, '身份证后 6 位不正确'
    return {
        "department": u["department"],
        "name": u["name"],
        "emp_id": u["emp_id"],
        "is_admin": bool(u.get("is_admin")),  # 兼容旧前端
        "role": u.get("role") or "staff",
        "org": u.get("org") or "johnson",
    }, None


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
        # GET 返回部门+人员清单(给前端下拉用),只暴露姓名
        try:
            from lib.users_store import list_departments, users_by_dept_public
            self._json(200, {
                "departments": list_departments(),
                "users_by_dept": users_by_dept_public(),
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
