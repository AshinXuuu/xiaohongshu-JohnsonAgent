"""
POST /api/login   —— 登录,签发会话 Token
GET  /api/login   —— 部门 + 人员清单(前端登录下拉用)

安全设计(2026-07 加固):
    - POST / GET 均有按 IP 的限流(独立于 AI 接口的配额),遏制花名册枚举 + 工号撞库。
    - 登录失败另有更严的失败闸:同 IP 连续失败会被锁,正常输错几次不受影响。
    - 管理员账号强制要求已配置身份证后 6 位(过渡期宽松仅适用于普通员工)。
    - 不再返回 Access-Control-Allow-Origin: *(同源页面无需 CORS,跨域一律拒)。
    - 500 只回通用文案,异常细节落服务端日志。
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sys
import json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.kv_store import log_event
from lib import ratelimit
from lib.users_store import ADMIN_ROLES

ROOT = Path(__file__).resolve().parent.parent

# 登录接口独立限流参数(与 AI 配额分开):
#   全量闸:同 IP 每分钟 8 次、每日 150 次(覆盖成功+失败,阻断脚本化枚举)。
#   失败闸:同 IP 每分钟 5 次失败、每日 60 次失败(正常人输错几次远达不到)。
_LOGIN_PER_MIN, _LOGIN_PER_DAY = 8, 150
_FAIL_PER_MIN, _FAIL_PER_DAY = 5, 60
_ROSTER_PER_MIN, _ROSTER_PER_DAY = 10, 300


def _client_ip(handler) -> str:
    """取真实客户端 IP:优先 Nginx 传来的 X-Forwarded-For(取第一个),
    否则用 socket 对端地址(本地直连时)。"""
    try:
        xff = handler.headers.get('X-Forwarded-For') or ''
        if xff:
            return xff.split(',')[0].strip()
        return handler.client_address[0]
    except Exception:
        return 'unknown'


def find_user(department, name, emp_id, id_last6=None):
    """白名单校验,匹配返回 user dict,不匹配返回 (None, reason)。
    数据源:数据库(lib/users_store);仅 DB 故障时回退 users.json。
    身份证后 6 位验证:
        - 已配置 → 必须匹配
        - 未配置 → 普通员工过渡期放行;管理员一律拒绝(必须先由超管在后台配置)
    """
    from lib.users_store import get_user
    from lib.idhash import verify_id6
    u = get_user(department, name, emp_id)
    if not u:
        return None, '工号或姓名与所选部门不匹配'
    v = verify_id6(id_last6, u.get("id_last6"))
    if v is False:
        return None, '身份证后 6 位不正确'
    role = u.get("role") or "staff"
    if v is None and role in ADMIN_ROLES:
        # 管理员权限大,不允许"未配置即放行"的过渡期宽松
        return None, '管理员账号必须先配置身份证后 6 位验证,请联系超级管理员在后台设置'
    return {
        "department": u["department"],
        "name": u["name"],
        "emp_id": u["emp_id"],
        "is_admin": bool(u.get("is_admin")),  # 兼容旧前端
        "role": role,
        "org": u.get("org") or "johnson",
    }, None


def log_user_action(req_info: dict):
    """打 print 日志(journalctl 可见),用于管理员追溯"""
    print(f"[LOGIN] {json.dumps(req_info, ensure_ascii=False)}", flush=True)


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        # 同源使用,无需放开跨域
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        # GET 返回部门+人员清单(给前端下拉用),只暴露姓名。
        # 有限流:防止匿名脚本反复抓全量花名册用于撞库。
        ip = _client_ip(self)
        ok, msg = ratelimit.check(None, ip, action='login_roster',
                                  per_min=_ROSTER_PER_MIN, per_day=_ROSTER_PER_DAY)
        if not ok:
            return self._json(429, {"error": msg})
        try:
            from lib.users_store import list_departments, users_by_dept_public
            self._json(200, {
                "departments": list_departments(),
                "users_by_dept": users_by_dept_public(),
            })
        except Exception as e:
            print(f"[LOGIN] GET 名单失败:{e}", flush=True)
            self._json(500, {"error": "服务暂时不可用,请稍后再试"})

    def do_POST(self):
        ip = _client_ip(self)
        # 全量闸(成功+失败都计)
        ok, msg = ratelimit.check(None, ip, action='login',
                                  per_min=_LOGIN_PER_MIN, per_day=_LOGIN_PER_DAY)
        if not ok:
            return self._json(429, {"error": msg})
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
                "ip": ip,
                "department": dept,
                "name": name,
                "emp_id": emp_id,
                "id6_provided": bool(id_last6),
                "success": user is not None,
                "fail_reason": err,
            })

            if not user:
                # 失败闸:只对失败计数;超限后该 IP 一段时间内直接拒绝
                fok, fmsg = ratelimit.check(None, ip, action='login_fail',
                                            per_min=_FAIL_PER_MIN, per_day=_FAIL_PER_DAY)
                if not fok:
                    return self._json(429, {"error": "失败次数过多,已暂时锁定,请稍后再试或联系管理员"})
                return self._json(401, {"error": err or "登录失败"})

            # 登录成功才记录用量事件(失败的不记)
            try:
                from lib.device import parse_ua
                dev = parse_ua(self.headers.get('User-Agent') or '')
            except Exception:
                dev = {'device': '未知', 'os': '其他'}
            log_event('login', user, {'device': dev['device'], 'os': dev['os']})
            from lib.session import issue_token
            token = issue_token(user)
            self._json(200, {"ok": True, "user": user, "token": token})

        except Exception as e:
            print(f"[LOGIN] POST 异常:{e}", flush=True)
            self._json(500, {"error": "服务暂时不可用,请稍后再试"})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
