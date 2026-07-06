"""POST /api/users —— 后台用户管理(仅管理员)。

请求体:{ "action": "list|add|update|deactivate", "_user": {...}, ... }

权限:
  - 一律服务端用 lib.auth 核对调用者是否管理员,不信任前端。
  - 只有 super_admin 才能授予/修改 管理员角色(org_admin / super_admin),防止越权提权。
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys
import json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.auth import is_admin, role_of
from lib import users_store


def _audit_log(caller, action, req):
    from lib.audit import log
    if action == 'add':
        s = f"新增用户 {req.get('name')}({req.get('department')} · {req.get('role') or 'staff'})"
    elif action == 'update':
        who = req.get('name') or f"id{req.get('id')}"
        s = f"编辑用户 {who}" + (f",角色→{req.get('role')}" if req.get('role') else "")
    elif action == 'deactivate':
        s = f"停用用户 {req.get('name') or ('id' + str(req.get('id')))}"
    elif action == 'reimport':
        s = "从名单重新导入用户(覆盖全部)"
    else:
        return
    log(caller, '用户', action, s)


VALID_ROLES = ("staff", "dept_manager", "org_admin", "super_admin")
ADMIN_ROLES = ("org_admin", "super_admin")


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            req = json.loads(body)

            from lib.session import user_from_headers
            caller = user_from_headers(self.headers)
            if not caller:
                return self._json(401, {"error": "未登录或登录已过期,请重新登录"})
            if not is_admin(caller):
                return self._json(403, {"error": "无权访问,仅管理员可操作"})
            caller_role = role_of(caller)
            action = (req.get("action") or "").strip()
            _audit_log(caller, action, req)

            if action == "list":
                return self._json(200, {
                    "users": users_store.all_users(),
                    "departments": users_store.list_departments(),
                    "roles": list(VALID_ROLES),
                    "caller_role": caller_role,
                })

            if action == "add":
                dept = (req.get("department") or "").strip()
                name = (req.get("name") or "").strip()
                emp_id = str(req.get("emp_id") or "").strip()
                id_last6 = str(req.get("id_last6") or "").strip()
                role = (req.get("role") or "staff").strip()
                if not (dept and name and emp_id):
                    return self._json(400, {"error": "请填写部门、姓名、工号"})
                if role not in VALID_ROLES:
                    return self._json(400, {"error": "角色不合法"})
                if role in ADMIN_ROLES and caller_role != "super_admin":
                    return self._json(403, {"error": "只有超级管理员能授予管理员角色"})
                # 同部门同工号同名查重
                if users_store.get_user(dept, name, emp_id):
                    return self._json(409, {"error": "该用户已存在"})
                uid = users_store.add_user(dept, name, emp_id, id_last6, role)
                return self._json(200, {"ok": True, "id": uid})

            if action == "update":
                uid = req.get("id")
                if not uid:
                    return self._json(400, {"error": "缺少用户 id"})
                fields = {}
                for k in ("department", "name", "emp_id", "id_last6"):
                    if k in req:
                        fields[k] = str(req.get(k) or "").strip()
                if "role" in req:
                    role = (req.get("role") or "").strip()
                    if role not in VALID_ROLES:
                        return self._json(400, {"error": "角色不合法"})
                    if role in ADMIN_ROLES and caller_role != "super_admin":
                        return self._json(403, {"error": "只有超级管理员能授予管理员角色"})
                    fields["role"] = role
                if not fields:
                    return self._json(400, {"error": "没有要更新的字段"})
                users_store.update_user(uid, **fields)
                return self._json(200, {"ok": True})

            if action == "deactivate":
                uid = req.get("id")
                if not uid:
                    return self._json(400, {"error": "缺少用户 id"})
                users_store.deactivate_user(uid)
                return self._json(200, {"ok": True})

            if action == "reimport":
                if caller_role != "super_admin":
                    return self._json(403, {"error": "仅超级管理员可从名单重新导入"})
                n = users_store.reimport_from_json()
                return self._json(200, {"ok": True, "count": n})

            return self._json(400, {"error": "未知 action"})
        except Exception as e:
            import traceback; traceback.print_exc()
            print("[API-500] " + getattr(self, "path", "") + " " + repr(e), flush=True)
            self._json(500, {"error": "服务器开小差了,请稍后重试"})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
