"""KOS 任务中心数据层(usage.db 内新增 4 张表)。

  kos_libraries   素材库(一个产品可有多个批次库)
  kos_materials   素材(主图 / 可拼图,指向 COS 对象 key)
  kos_tasks       任务(管理员发起,绑定一个库)
  kos_packs       领取记录 = 严格唯一组合消耗 + 完成回填

容量与唯一性(严格,不重复消耗):
  每人每篇成品 = 1 封面(主图→封面生成,不占组合)+ 1 个「2合1」+ 1 个「4合1」。
  组合以"可拼图源图的无序集合"为单位:2合1 = 一对;4合1 = 一组四张。
  单库可支持「人·篇」总数 = min(C(n,2), C(n,4)),n = 该库在用可拼图数(需 n≥4)。
  每发一份,占用一对 + 一组四张,永不复用。
"""
import json
import math
import os
import random
import sqlite3
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get('USAGE_DB_PATH', str(_ROOT / 'data' / 'usage.db')))

_lock = threading.Lock()
_schema_ready = False

ROLE_MAIN = '主图'
ROLE_TILE = '可拼图'


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    c.execute('PRAGMA journal_mode=WAL')
    c.row_factory = sqlite3.Row
    return c


def _ensure():
    global _schema_ready
    if _schema_ready:
        return
    with _lock:
        if _schema_ready:
            return
        c = _conn()
        try:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS kos_libraries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    org TEXT NOT NULL DEFAULT 'johnson',
                    brand TEXT NOT NULL, product TEXT NOT NULL,
                    code TEXT, note TEXT,
                    cos_prefix TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS kos_materials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    library_id INTEGER NOT NULL,
                    role TEXT NOT NULL,              -- 主图 / 可拼图
                    idx INTEGER NOT NULL DEFAULT 0,
                    cos_key TEXT NOT NULL,
                    filename TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_kmat_lib ON kos_materials(library_id, role);

                CREATE TABLE IF NOT EXISTS kos_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    org TEXT NOT NULL DEFAULT 'johnson',
                    title TEXT, brand TEXT, product TEXT,
                    library_id INTEGER NOT NULL,
                    scope TEXT NOT NULL DEFAULT 'all',   -- all / dept
                    depts TEXT,                          -- json 数组(scope=dept 时)
                    per_person INTEGER NOT NULL DEFAULT 1,
                    deadline TEXT,
                    created_by TEXT,
                    status TEXT NOT NULL DEFAULT 'open',  -- open / closed
                    created_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS kos_packs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER, library_id INTEGER NOT NULL,
                    emp_id TEXT, user_name TEXT, department TEXT,
                    post_index INTEGER,
                    cover_material_id INTEGER,
                    combo2 TEXT, combo4 TEXT,            -- json:有序 material id 列表
                    copy_json TEXT,                     -- 文案快照
                    note_url TEXT,
                    status TEXT NOT NULL DEFAULT 'issued', -- issued / published
                    created_at INTEGER, published_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_kpack_lib  ON kos_packs(library_id);
                CREATE INDEX IF NOT EXISTS idx_kpack_task ON kos_packs(task_id);
                CREATE INDEX IF NOT EXISTS idx_kpack_user ON kos_packs(emp_id);
            """)
            c.commit()
            _schema_ready = True
        finally:
            c.close()


# ──────────────── 素材库 / 素材 ────────────────

def create_library(brand, product, code='', note='', cos_prefix='', org='johnson'):
    _ensure()
    c = _conn()
    try:
        cur = c.execute("INSERT INTO kos_libraries(org,brand,product,code,note,cos_prefix,active,created_at) "
                        "VALUES(?,?,?,?,?,?,1,?)", (org, brand.strip(), product.strip(),
                                                    code.strip(), note.strip(), cos_prefix.strip(), int(time.time())))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def get_library(library_id):
    _ensure()
    c = _conn()
    try:
        r = c.execute("SELECT * FROM kos_libraries WHERE id=?", (library_id,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def existing_cos_keys(library_id):
    """该库已登记的 cos_key 集合(扫描登记时去重用)。"""
    _ensure()
    c = _conn()
    try:
        return {r['cos_key'] for r in c.execute(
            "SELECT cos_key FROM kos_materials WHERE library_id=? AND active=1", (library_id,)).fetchall()}
    finally:
        c.close()


def deactivate_library(library_id):
    """停用素材库(软删除,记录保留;COS 原图不动)。"""
    _ensure()
    c = _conn()
    try:
        c.execute("UPDATE kos_libraries SET active=0 WHERE id=?", (library_id,))
        c.commit()
        return True
    finally:
        c.close()


def deactivate_material(mid):
    _ensure()
    c = _conn()
    try:
        c.execute("UPDATE kos_materials SET active=0 WHERE id=?", (mid,))
        c.commit()
        return True
    finally:
        c.close()


def list_libraries(brand=None, product=None):
    _ensure()
    c = _conn()
    try:
        q = "SELECT * FROM kos_libraries WHERE active=1"
        args = []
        if brand:
            q += " AND brand=?"; args.append(brand)
        if product:
            q += " AND product=?"; args.append(product)
        q += " ORDER BY created_at DESC"
        libs = [dict(r) for r in c.execute(q, args).fetchall()]
        for lib in libs:
            lib['capacity'] = capacity(lib['id'])
        return libs
    finally:
        c.close()


def add_material(library_id, role, cos_key, filename='', idx=0):
    _ensure()
    c = _conn()
    try:
        cur = c.execute("INSERT INTO kos_materials(library_id,role,idx,cos_key,filename,active,created_at) "
                        "VALUES(?,?,?,?,?,1,?)", (library_id, role, idx, cos_key.strip(),
                                                  filename.strip(), int(time.time())))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def list_materials(library_id, role=None):
    _ensure()
    c = _conn()
    try:
        q = "SELECT * FROM kos_materials WHERE library_id=? AND active=1"
        args = [library_id]
        if role:
            q += " AND role=?"; args.append(role)
        q += " ORDER BY role, idx, id"
        return [dict(r) for r in c.execute(q, args).fetchall()]
    finally:
        c.close()


def _tile_ids(c, library_id):
    rows = c.execute("SELECT id FROM kos_materials WHERE library_id=? AND role=? AND active=1 ORDER BY id",
                     (library_id, ROLE_TILE)).fetchall()
    return [r['id'] for r in rows]


def _main_ids(c, library_id):
    rows = c.execute("SELECT id FROM kos_materials WHERE library_id=? AND role=? AND active=1 ORDER BY id",
                     (library_id, ROLE_MAIN)).fetchall()
    return [r['id'] for r in rows]


def _cap_from_n(n):
    """单库可支持「人·篇」总数:每份需一对(2合1)+ 一组四张(4合1)。需 n≥4。"""
    if n < 4:
        return 0
    return min(math.comb(n, 2), math.comb(n, 4))


def capacity(library_id):
    """返回 {tiles, mains, total, used, remaining, enough_min}。"""
    _ensure()
    c = _conn()
    try:
        tiles = _tile_ids(c, library_id)
        mains = _main_ids(c, library_id)
        n = len(tiles)
        total = _cap_from_n(n)
        used = c.execute("SELECT COUNT(*) FROM kos_packs WHERE library_id=?", (library_id,)).fetchone()[0]
        return {
            "tiles": n, "mains": len(mains),
            "total": total, "used": used, "remaining": max(0, total - used),
            "need_tiles_min": 4,
        }
    finally:
        c.close()


# ──────────────── 严格唯一组合选取 ────────────────

def _used_combos(c, library_id, col):
    out = set()
    for r in c.execute(f"SELECT {col} FROM kos_packs WHERE library_id=? AND {col} IS NOT NULL",
                       (library_id,)).fetchall():
        try:
            out.add(tuple(sorted(json.loads(r[col]))))
        except Exception:
            pass
    return out


def pick_combo(library_id, rng=None):
    """为一份成品挑一组未用过的素材:返回 {cover, combo2, combo4} 的 material id,
    或 None(库已发尽 / 可拼图不足)。不在此处落库;调用方在事务里 record_pack 占用。"""
    _ensure()
    rng = rng or random.Random()
    c = _conn()
    try:
        tiles = _tile_ids(c, library_id)
        mains = _main_ids(c, library_id)
        n = len(tiles)
        if n < 4 or not mains:
            return None
        used2 = _used_combos(c, library_id, 'combo2')
        used4 = _used_combos(c, library_id, 'combo4')
        if len(used2) >= math.comb(n, 2) or len(used4) >= math.comb(n, 4):
            return None
        pair = _sample_unused(tiles, 2, used2, rng)
        quad = _sample_unused(tiles, 4, used4, rng)
        if pair is None or quad is None:
            return None
        cover = rng.choice(mains)
        return {"cover": cover, "combo2": list(pair), "combo4": list(quad)}
    finally:
        c.close()


def _sample_unused(ids, k, used_set, rng, max_try=2000):
    """随机抽 k 个不重复 id,其 sorted 元组不在 used_set 里。抽不到返回 None。"""
    for _ in range(max_try):
        pick = tuple(sorted(rng.sample(ids, k)))
        if pick not in used_set:
            return pick
    # 兜底:穷举(n 小,组合不多)
    from itertools import combinations
    candidates = [t for t in combinations(sorted(ids), k) if t not in used_set]
    if not candidates:
        return None
    return rng.choice(candidates)


def record_pack(library_id, combo, task_id=None, user=None, post_index=0, copy_json=None):
    """把选好的组合落库占用(完成唯一性消耗)。combo 为 pick_combo 的返回。"""
    _ensure()
    user = user or {}
    c = _conn()
    try:
        cur = c.execute(
            "INSERT INTO kos_packs(task_id,library_id,emp_id,user_name,department,post_index,"
            "cover_material_id,combo2,combo4,copy_json,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'issued',?)",
            (task_id, library_id, user.get('emp_id'), user.get('name'), user.get('department'),
             post_index, combo['cover'], json.dumps(combo['combo2']), json.dumps(combo['combo4']),
             json.dumps(copy_json) if copy_json is not None else None, int(time.time())))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


# ──────────────── 任务 ────────────────

def create_task(title, brand, product, library_id, scope='all', depts=None,
                per_person=1, deadline='', created_by='', org='johnson'):
    _ensure()
    c = _conn()
    try:
        cur = c.execute(
            "INSERT INTO kos_tasks(org,title,brand,product,library_id,scope,depts,per_person,"
            "deadline,created_by,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,'open',?)",
            (org, (title or '').strip(), brand, product, library_id, scope,
             json.dumps(depts or []), max(1, int(per_person or 1)), (deadline or '').strip(),
             created_by, int(time.time())))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def get_task(task_id):
    _ensure()
    c = _conn()
    try:
        r = c.execute("SELECT * FROM kos_tasks WHERE id=?", (task_id,)).fetchone()
        if not r:
            return None
        t = dict(r)
        t['depts'] = json.loads(t.get('depts') or '[]')
        return t
    finally:
        c.close()


def close_task(task_id):
    _ensure()
    c = _conn()
    try:
        c.execute("UPDATE kos_tasks SET status='closed' WHERE id=?", (task_id,))
        c.commit()
        return True
    finally:
        c.close()


def list_tasks(org='johnson'):
    _ensure()
    c = _conn()
    try:
        tasks = [dict(r) for r in c.execute(
            "SELECT * FROM kos_tasks WHERE org=? ORDER BY created_at DESC", (org,)).fetchall()]
        for t in tasks:
            t['depts'] = json.loads(t.get('depts') or '[]')
            t['issued'] = c.execute("SELECT COUNT(*) FROM kos_packs WHERE task_id=?", (t['id'],)).fetchone()[0]
            t['published'] = c.execute(
                "SELECT COUNT(*) FROM kos_packs WHERE task_id=? AND status='published'", (t['id'],)).fetchone()[0]
            t['capacity'] = capacity(t['library_id'])
        return tasks
    finally:
        c.close()


# ──────────────── 业务侧 ────────────────

def tasks_for_user(user):
    """该业务能看到的进行中任务 + 自己的进度。"""
    _ensure()
    dept = (user or {}).get('department')
    emp = (user or {}).get('emp_id')
    c = _conn()
    try:
        rows = c.execute("SELECT * FROM kos_tasks WHERE org='johnson' AND status='open' ORDER BY created_at DESC").fetchall()
        out = []
        for r in rows:
            t = dict(r)
            depts = json.loads(t.get('depts') or '[]')
            if t['scope'] == 'dept' and dept not in depts:
                continue
            t['depts'] = depts
            t['my_issued'] = c.execute(
                "SELECT COUNT(*) FROM kos_packs WHERE task_id=? AND emp_id=?", (t['id'], emp)).fetchone()[0]
            t['my_published'] = c.execute(
                "SELECT COUNT(*) FROM kos_packs WHERE task_id=? AND emp_id=? AND status='published'",
                (t['id'], emp)).fetchone()[0]
            t['remaining_cap'] = capacity(t['library_id'])['remaining']
            out.append(t)
        return out
    finally:
        c.close()


def count_user_task_packs(task_id, emp_id):
    _ensure()
    c = _conn()
    try:
        return c.execute("SELECT COUNT(*) FROM kos_packs WHERE task_id=? AND emp_id=?",
                         (task_id, emp_id)).fetchone()[0]
    finally:
        c.close()


def get_pack(pack_id):
    _ensure()
    c = _conn()
    try:
        r = c.execute("SELECT * FROM kos_packs WHERE id=?", (pack_id,)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def publish_pack(pack_id, emp_id, note_url):
    """业务回填小红书链接 → 标记已发布。仅本人可操作。"""
    _ensure()
    c = _conn()
    try:
        r = c.execute("SELECT emp_id FROM kos_packs WHERE id=?", (pack_id,)).fetchone()
        if not r or r['emp_id'] != emp_id:
            return False
        c.execute("UPDATE kos_packs SET status='published', note_url=?, published_at=? WHERE id=?",
                  ((note_url or '').strip(), int(time.time()), pack_id))
        c.commit()
        return True
    finally:
        c.close()


def my_packs(emp_id, task_id=None):
    _ensure()
    c = _conn()
    try:
        q = "SELECT * FROM kos_packs WHERE emp_id=?"
        args = [emp_id]
        if task_id:
            q += " AND task_id=?"; args.append(task_id)
        q += " ORDER BY created_at DESC"
        return [dict(r) for r in c.execute(q, args).fetchall()]
    finally:
        c.close()


def leaderboard(org='johnson'):
    """排行:每人 笔记数(已发布)+ 完成任务数(某任务已发布≥该任务每人篇数)。"""
    _ensure()
    c = _conn()
    try:
        per = {t['id']: t['per_person'] for t in
               (dict(r) for r in c.execute("SELECT id,per_person FROM kos_tasks WHERE org=?", (org,)).fetchall())}
        rows = c.execute(
            "SELECT emp_id, user_name, department, task_id, COUNT(*) c "
            "FROM kos_packs WHERE status='published' GROUP BY emp_id, task_id").fetchall()
        agg = {}
        for r in rows:
            e = r['emp_id'] or ''
            a = agg.setdefault(e, {"emp_id": e, "name": r['user_name'], "department": r['department'],
                                   "notes": 0, "tasks_done": 0})
            a["notes"] += r['c']
            if r['c'] >= per.get(r['task_id'], 1):
                a["tasks_done"] += 1
        board = sorted(agg.values(), key=lambda x: (-x["tasks_done"], -x["notes"]))
        return board
    finally:
        c.close()
