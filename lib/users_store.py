"""用户数据存储 —— 把原本手改的 data/users.json 收进数据库(usage.db 的 app_users 表)。

目标:后台可视化增删改用户,不再手动编辑 JSON 文件 + git push + 服务器拉取。

策略:
  - 首次访问时,若 app_users 表为空,自动从 users.json 播种一次(保留下拉顺序)。
  - 之后以数据库为准;后台 UI 的增删改直接写库。
兜底(关键):数据库不可用 / 为空且播种失败时,读函数自动回退直接读 users.json,
            保证登录这条命门永不挂。
"""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get('USAGE_DB_PATH', str(_ROOT / 'data' / 'usage.db')))
USERS_JSON = _ROOT / 'data' / 'users.json'

_lock = threading.Lock()
_seeded = False

ADMIN_ROLES = ('org_admin', 'super_admin')


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    c.execute('PRAGMA journal_mode=WAL')
    c.row_factory = sqlite3.Row
    return c


def _ensure_schema(c):
    c.executescript("""
        CREATE TABLE IF NOT EXISTS app_users (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            org         TEXT NOT NULL DEFAULT 'johnson',
            department  TEXT NOT NULL,
            name        TEXT NOT NULL,
            emp_id      TEXT NOT NULL,
            id_last6    TEXT,
            role        TEXT NOT NULL DEFAULT 'staff',
            is_admin    INTEGER NOT NULL DEFAULT 0,
            active      INTEGER NOT NULL DEFAULT 1,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            created_at  INTEGER,
            updated_at  INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_users_dept ON app_users(org, department);
        CREATE INDEX IF NOT EXISTS idx_users_emp  ON app_users(org, emp_id);
    """)


def _load_json():
    try:
        return json.loads(USERS_JSON.read_text(encoding='utf-8'))
    except Exception:
        return {"departments": [], "users_by_dept": {}}


def _role_of_record(u):
    return u.get('role') or ('org_admin' if u.get('is_admin') else 'staff')


def _ensure_seeded():
    """首次调用时建表 + 空表则从 JSON 播种。失败也置位,读函数会回退 JSON。"""
    global _seeded
    if _seeded:
        return
    with _lock:
        if _seeded:
            return
        try:
            c = _conn()
            try:
                _ensure_schema(c)
                n = c.execute("SELECT COUNT(*) FROM app_users").fetchone()[0]
                if n == 0:
                    data = _load_json()
                    by_dept = data.get('users_by_dept', {})
                    # 保留 departments 顺序,其余追加
                    seq = list(data.get('departments') or [])
                    seq += [d for d in by_dept if d not in seq]
                    now = int(time.time())
                    order = 0
                    for dept in seq:
                        for u in by_dept.get(dept, []):
                            c.execute(
                                "INSERT INTO app_users(org,department,name,emp_id,id_last6,"
                                "role,is_admin,active,sort_order,created_at,updated_at) "
                                "VALUES(?,?,?,?,?,?,?,1,?,?,?)",
                                (u.get('org') or 'johnson', dept, (u.get('name') or '').strip(),
                                 str(u.get('emp_id') or '').strip(), _hash_id6(u.get('id_last6')),
                                 _role_of_record(u), 1 if u.get('is_admin') else 0, order, now, now))
                            order += 1
                    c.commit()
                _seeded = True
            finally:
                c.close()
        except Exception:
            _seeded = True  # 不再重试;读函数回退 JSON


def _row_to_user(r):
    # 内部用:保留存储的 id_last6(现为哈希),登录校验需要它。
    # 面向后台/客户端的 all_users 会把该值抹掉,只留 has_id6 布尔。
    return {
        "id": r["id"],
        "department": r["department"], "name": r["name"], "emp_id": r["emp_id"],
        "id_last6": r["id_last6"], "has_id6": bool((r["id_last6"] or "").strip()),
        "role": r["role"], "is_admin": bool(r["is_admin"]),
        "org": r["org"], "active": bool(r["active"]),
    }


def _hash_id6(raw):
    """写库前把后 6 位转成加盐哈希。

    ⚠️ 安全修复(2026-07):不再静默降级存明文。密钥未配置属于部署错误,
    应当报错暴露(启动时 dev_server 已强制校验 ID6_SALT,正常运行不会走到)。"""
    from lib.idhash import hash_id6
    return hash_id6(raw)


# ──────────────── 读 ────────────────

def get_user(department, name, emp_id):
    """登录 / 鉴权:精确匹配 部门+姓名+工号 的在职用户;无则 None。

    JSON 回退只在「数据库本身不可用」时启用。
    ⚠️ 安全修复(2026-07):DB 正常但查无此人(含已停用 active=0)时必须返回 None,
    不得再落入 JSON 兜底 —— 否则后台停用/删除的用户(尤其管理员)只要还留在
    data/users.json 里就能照常登录,停用形同虚设。"""
    dept = (department or '').strip()
    name = (name or '').strip()
    emp = str(emp_id or '').strip()
    if not (dept and name and emp):
        return None
    _ensure_seeded()
    try:
        c = _conn()
        try:
            r = c.execute(
                "SELECT * FROM app_users WHERE department=? AND name=? AND emp_id=? AND active=1 LIMIT 1",
                (dept, name, emp)).fetchone()
            # DB 可用:查到返回,查不到就是没有/已停用,直接拒绝,不回退 JSON。
            return _row_to_user(r) if r else None
        finally:
            c.close()
    except Exception as e:
        print(f"[USERS] 数据库不可用,get_user 回退 users.json:{e}", flush=True)
    # 仅 DB 异常才回退 JSON(保证基础设施故障时登录命门不挂)
    for u in _load_json().get('users_by_dept', {}).get(dept, []):
        if (u.get('name') or '').strip() == name and str(u.get('emp_id') or '').strip() == emp:
            return {"department": dept, "name": name, "emp_id": emp,
                    "id_last6": (u.get('id_last6') or '').strip(), "role": _role_of_record(u),
                    "is_admin": bool(u.get('is_admin')), "org": u.get('org') or 'johnson', "active": True}
    return None


def list_departments():
    _ensure_seeded()
    try:
        c = _conn()
        try:
            rows = c.execute(
                "SELECT department, MIN(sort_order) o FROM app_users WHERE active=1 "
                "GROUP BY department ORDER BY o").fetchall()
            if rows:
                return [r["department"] for r in rows]
        finally:
            c.close()
    except Exception:
        pass
    d = _load_json()
    return d.get('departments') or list(d.get('users_by_dept', {}).keys())


def users_by_dept_public():
    """登录下拉:{dept:[{name}]},只暴露姓名。"""
    _ensure_seeded()
    try:
        c = _conn()
        try:
            rows = c.execute(
                "SELECT department, name FROM app_users WHERE active=1 ORDER BY sort_order").fetchall()
            if rows:
                out = {}
                for r in rows:
                    out.setdefault(r["department"], []).append({"name": r["name"]})
                return out
        finally:
            c.close()
    except Exception:
        pass
    d = _load_json()
    return {dept: [{"name": u.get("name")} for u in lst]
            for dept, lst in d.get('users_by_dept', {}).items()}


def all_users():
    """全部在职用户(含 emp_id/部门/角色),给 admin 富化 / 管理列表用。"""
    _ensure_seeded()
    try:
        c = _conn()
        try:
            rows = c.execute("SELECT * FROM app_users WHERE active=1 ORDER BY sort_order").fetchall()
            if rows:
                # 面向后台展示:抹掉 id_last6 哈希值,只保留 has_id6
                return [{**_row_to_user(r), "id_last6": ""} for r in rows]
        finally:
            c.close()
    except Exception:
        pass
    out = []
    for dept, lst in _load_json().get('users_by_dept', {}).items():
        for u in lst:
            out.append({"department": dept, "name": u.get('name'), "emp_id": str(u.get('emp_id') or ''),
                        "id_last6": "", "has_id6": bool((u.get('id_last6') or '').strip()),
                        "role": _role_of_record(u),
                        "is_admin": bool(u.get('is_admin')), "org": u.get('org') or 'johnson', "active": True})
    return out


# ──────────────── 写(后台 UI 用)────────────────

def add_user(department, name, emp_id, id_last6='', role='staff', org='johnson'):
    _ensure_seeded()
    now = int(time.time())
    c = _conn()
    try:
        mx = c.execute("SELECT COALESCE(MAX(sort_order), 0) FROM app_users").fetchone()[0]
        cur = c.execute(
            "INSERT INTO app_users(org,department,name,emp_id,id_last6,role,is_admin,active,"
            "sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,?,?,?)",
            (org, department.strip(), name.strip(), str(emp_id).strip(), _hash_id6(id_last6),
             role, 1 if role in ADMIN_ROLES else 0, mx + 1, now, now))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def update_user(uid, **fields):
    _ensure_seeded()
    allowed = {'department', 'name', 'emp_id', 'id_last6', 'role', 'active', 'org'}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            if k == 'id_last6':
                v = _hash_id6(v)   # 后台改身份证后6位:同样存哈希
            sets.append(f"{k}=?")
            vals.append(v)
    if 'role' in fields:
        sets.append("is_admin=?")
        vals.append(1 if fields['role'] in ADMIN_ROLES else 0)
    if not sets:
        return False
    sets.append("updated_at=?")
    vals.append(int(time.time()))
    vals.append(uid)
    c = _conn()
    try:
        c.execute(f"UPDATE app_users SET {', '.join(sets)} WHERE id=?", vals)
        c.commit()
        return True
    finally:
        c.close()


def deactivate_user(uid):
    """软删除:停用而非物理删除,保留历史可追溯。"""
    return update_user(uid, active=0)


def reimport_from_json():
    """从 users.json 强制重建 app_users(清空再播种)。给后台「从名单重新导入」用:
    更新 users.json 后,超管点一下即可把数据库刷新成新名单。会覆盖库里所有用户。"""
    c = _conn()
    try:
        _ensure_schema(c)
        c.execute("DELETE FROM app_users")
        data = _load_json()
        by_dept = data.get('users_by_dept', {})
        seq = list(data.get('departments') or [])
        seq += [d for d in by_dept if d not in seq]
        now = int(time.time())
        order = 0
        n = 0
        for dept in seq:
            for u in by_dept.get(dept, []):
                c.execute(
                    "INSERT INTO app_users(org,department,name,emp_id,id_last6,role,is_admin,active,"
                    "sort_order,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,?,?,?)",
                    (u.get('org') or 'johnson', dept, (u.get('name') or '').strip(),
                     str(u.get('emp_id') or '').strip(), _hash_id6(u.get('id_last6')),
                     _role_of_record(u), 1 if u.get('is_admin') else 0, order, now, now))
                order += 1
                n += 1
        c.commit()
        return n
    finally:
        c.close()
