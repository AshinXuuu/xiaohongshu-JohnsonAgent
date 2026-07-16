"""KOS 任务中心数据层(usage.db 内新增 4 张表)。

  kos_libraries   素材库(一个产品可有多个批次库)
  kos_materials   素材(主图 / 2合1横版 / 4合1竖版,指向 COS 对象 key)
  kos_tasks       任务(管理员发起,绑定一个库)
  kos_packs       领取记录 = 严格唯一组合消耗 + 完成回填

素材五分类(2026-07 起新库使用;旧三分类库按旧逻辑发完为止):
  主图      —— 一张原图直出(可复用,不计消耗)
  横版近景 / 横版远景 —— 2合1:近、远景各 1 张上下拼(一份占一对,唯一不复用)
  竖版近景 / 竖版远景 —— 4合1:竖近 2 张 + 竖远 2 张田字拼(唯一不复用)

容量(五分类):min( 横近×横远, C(竖近,2)×C(竖远,2) );需 横近≥1、横远≥1、竖近≥2、竖远≥2。
容量(旧三分类):min( C(横版,2), C(竖版,4) )。
组合以"源图的无序集合"为单位入库(sorted id 元组),唯一索引兜底,永不复用。
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
ROLE_TWO = '2合1'      # 旧三分类:横版,上下拼(遗留库继续可用)
ROLE_FOUR = '4合1'     # 旧三分类:竖版,田字拼(遗留库继续可用)
ROLE_TILE = '可拼图'   # 更早遗留分类(已弃用,仅兼容历史数据读取)
# 2026-07 五分类(新库使用):2合1 = 横近×横远 各 1 张;4合1 = 竖近 2 张 + 竖远 2 张
ROLE_TWO_NEAR = '横版近景'
ROLE_TWO_FAR = '横版远景'
ROLE_FOUR_NEAR = '竖版近景'
ROLE_FOUR_FAR = '竖版远景'
SPLIT_ROLES = (ROLE_TWO_NEAR, ROLE_TWO_FAR, ROLE_FOUR_NEAR, ROLE_FOUR_FAR)


def _conn():
    # 统一走 lib/db 的线程级复用连接(2026-07 性能加固,PRAGMA 见 lib/db.py)
    from lib.db import get_conn
    return get_conn()


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

                CREATE TABLE IF NOT EXISTS kos_self_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    org TEXT NOT NULL DEFAULT 'johnson',
                    emp_id TEXT, user_name TEXT, department TEXT,
                    note_url TEXT NOT NULL,
                    created_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_kself_user ON kos_self_posts(emp_id);
            """)
            c.commit()
            # 唯一约束(2026-07 并发修复):同一素材库内 2合1/4合1 组合不得重复发放。
            # 历史数据若已有重复(此前的竞态窗口造成),索引会建不上——只告警不阻断,
            # 管理员按日志人工去重后,下次启动会自动补建。
            for col in ('combo2', 'combo4'):
                try:
                    c.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS uq_kpack_{col} "
                              f"ON kos_packs(library_id, {col})")
                    c.commit()
                except Exception as e:
                    print(f"[KOS] 警告:唯一索引 uq_kpack_{col} 创建失败(可能存在历史重复组合):{e}。"
                          f"排查:SELECT library_id,{col},COUNT(*) FROM kos_packs "
                          f"GROUP BY library_id,{col} HAVING COUNT(*)>1;", flush=True)
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


def _ids_by_role(c, library_id, role):
    rows = c.execute("SELECT id FROM kos_materials WHERE library_id=? AND role=? AND active=1 ORDER BY id",
                     (library_id, role)).fetchall()
    return [r['id'] for r in rows]


def _main_ids(c, library_id):
    return _ids_by_role(c, library_id, ROLE_MAIN)


def _cap_from(h, v):
    """旧三分类容量:每份需一对横版 + 一组四张竖版。需 h≥2 且 v≥4。"""
    if h < 2 or v < 4:
        return 0
    return min(math.comb(h, 2), math.comb(v, 4))


def _pools(c, library_id):
    """素材池与模式判定。
    新五分类库('split'):有任一 近/远景 素材即按新逻辑;
    旧三分类库('legacy'):只有 2合1/4合1 素材,按旧逻辑继续发完。
    注意:旧库一旦上传了新分类素材即切换新逻辑,旧 2合1/4合1 素材不再参与拼图
    (后台按钮层面已引导:旧库建议发完关闭、新库用五分类)。"""
    tn = _ids_by_role(c, library_id, ROLE_TWO_NEAR)
    tf = _ids_by_role(c, library_id, ROLE_TWO_FAR)
    fn = _ids_by_role(c, library_id, ROLE_FOUR_NEAR)
    ff = _ids_by_role(c, library_id, ROLE_FOUR_FAR)
    if tn or tf or fn or ff:
        return 'split', {"tn": tn, "tf": tf, "fn": fn, "ff": ff}
    return 'legacy', {"two": _ids_by_role(c, library_id, ROLE_TWO),
                      "four": _ids_by_role(c, library_id, ROLE_FOUR)}


def _cap_split(tn, tf, fn, ff):
    """五分类容量:2合1 = 近×远;4合1 = C(竖近,2)×C(竖远,2)。
    需 横近≥1、横远≥1、竖近≥2、竖远≥2。"""
    if tn < 1 or tf < 1 or fn < 2 or ff < 2:
        return 0
    return min(tn * tf, math.comb(fn, 2) * math.comb(ff, 2))


def capacity(library_id):
    """返回容量信息;two/four 为横/竖版合计(新旧模式通用),split 模式额外带
    two_near/two_far/four_near/four_far。tiles 保留兼容。"""
    _ensure()
    c = _conn()
    try:
        mains = _main_ids(c, library_id)
        mode, P = _pools(c, library_id)
        used = c.execute("SELECT COUNT(*) FROM kos_packs WHERE library_id=?", (library_id,)).fetchone()[0]
        if mode == 'split':
            tn, tf, fn, ff = len(P['tn']), len(P['tf']), len(P['fn']), len(P['ff'])
            total = _cap_split(tn, tf, fn, ff)
            return {
                "mode": "split", "mains": len(mains),
                "two_near": tn, "two_far": tf, "four_near": fn, "four_far": ff,
                "two": tn + tf, "four": fn + ff, "tiles": tn + tf + fn + ff,
                "total": total, "used": used, "remaining": max(0, total - used),
                "need_two_min": 2, "need_four_min": 4,
            }
        h, v = len(P['two']), len(P['four'])
        total = _cap_from(h, v)
        return {
            "mode": "legacy", "mains": len(mains), "two": h, "four": v, "tiles": h + v,
            "total": total, "used": used, "remaining": max(0, total - used),
            "need_two_min": 2, "need_four_min": 4,
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


def _sample_unused(ids, k, used_set, rng, max_try=2000):
    """随机抽 k 个不重复 id,其 sorted 元组不在 used_set 里。抽不到返回 None。"""
    for _ in range(max_try):
        pick = tuple(sorted(rng.sample(ids, k)))
        if pick not in used_set:
            return pick
    # 兜底:水塘抽样遍历全部组合(O(1) 内存;此前一次性物化列表,
    # 竖版上百张时可膨胀到数 GB —— 恰好在"随机命中率低"的将发尽场景触发)
    from itertools import combinations
    chosen, seen = None, 0
    for t in combinations(sorted(ids), k):
        if t in used_set:
            continue
        seen += 1
        if rng.randrange(seen) == 0:
            chosen = t
    return chosen


def _sample_pair_split(near, far, used_set, rng, max_try=2000):
    """五分类 2合1:近景、远景各取 1 张,sorted 二元组不在 used_set。"""
    for _ in range(max_try):
        pick = tuple(sorted((rng.choice(near), rng.choice(far))))
        if pick not in used_set:
            return pick
    from itertools import product
    chosen, seen = None, 0
    for a, b in product(near, far):
        t = tuple(sorted((a, b)))
        if t in used_set:
            continue
        seen += 1
        if rng.randrange(seen) == 0:
            chosen = t
    return chosen


def _sample_quad_split(near, far, used_set, rng, max_try=2000):
    """五分类 4合1:竖近取 2 张 + 竖远取 2 张,sorted 四元组不在 used_set。"""
    for _ in range(max_try):
        pick = tuple(sorted(rng.sample(near, 2) + rng.sample(far, 2)))
        if pick not in used_set:
            return pick
    from itertools import combinations, product
    chosen, seen = None, 0
    for n2, f2 in product(combinations(sorted(near), 2), combinations(sorted(far), 2)):
        t = tuple(sorted(n2 + f2))
        if t in used_set:
            continue
        seen += 1
        if rng.randrange(seen) == 0:
            chosen = t
    return chosen


def claim_pack(library_id, task_id, user, per_person, copy_json=None, rng=None):
    """原子领取:在**同一连接、同一写事务**里完成
        「每人限额校验 → 选未用组合 → INSERT 占用」,
    并靠 uq_kpack_combo2/4 唯一索引兜底,撞车自动重选(最多 3 次)。

    为什么(2026-07 并发修复):此前 pick_combo 与 record_pack 分连接分事务,
    中间还夹着 COS 下载 + LLM 文案(秒级窗口),周四全员同时领取会拿到同一组合,
    破坏「唯一不复用」并超发限额。现在调用方应先 claim 再做下载/出图,
    失败用 delete_pack 回滚。

    返回 (status, data):
        ('ok', {pack_id, combo, post_index})
        ('limit', 已领数) / ('exhausted', None) / ('error', 原因)
    """
    _ensure()
    user = user or {}
    rng = rng or random.Random()
    emp = user.get('emp_id')
    for attempt in range(3):
        c = _conn()
        try:
            c.execute("BEGIN IMMEDIATE")      # 拿写锁,串行化并发领取
            mine = c.execute("SELECT COUNT(*) FROM kos_packs WHERE task_id=? AND emp_id=?",
                             (task_id, emp)).fetchone()[0]
            if per_person and mine >= per_person:
                c.execute("ROLLBACK")
                return 'limit', mine
            mains = _main_ids(c, library_id)
            mode, P = _pools(c, library_id)
            used2 = _used_combos(c, library_id, 'combo2')
            used4 = _used_combos(c, library_id, 'combo4')
            if mode == 'split':
                # 五分类:2合1 = 横近×横远;4合1 = 竖近2 + 竖远2
                tn, tf, fn, ff = P['tn'], P['tf'], P['fn'], P['ff']
                if (not mains or _cap_split(len(tn), len(tf), len(fn), len(ff)) == 0
                        or len(used2) >= len(tn) * len(tf)
                        or len(used4) >= math.comb(len(fn), 2) * math.comb(len(ff), 2)):
                    c.execute("ROLLBACK")
                    return 'exhausted', None
                pair = _sample_pair_split(tn, tf, used2, rng)
                quad = _sample_quad_split(fn, ff, used4, rng)
            else:
                two, four = P['two'], P['four']
                if not mains or len(two) < 2 or len(four) < 4:
                    c.execute("ROLLBACK")
                    return 'exhausted', None
                if len(used2) >= math.comb(len(two), 2) or len(used4) >= math.comb(len(four), 4):
                    c.execute("ROLLBACK")
                    return 'exhausted', None
                pair = _sample_unused(two, 2, used2, rng)
                quad = _sample_unused(four, 4, used4, rng)
            if pair is None or quad is None:
                c.execute("ROLLBACK")
                return 'exhausted', None
            combo = {"cover": rng.choice(mains), "combo2": list(pair), "combo4": list(quad)}
            cur = c.execute(
                "INSERT INTO kos_packs(task_id,library_id,emp_id,user_name,department,post_index,"
                "cover_material_id,combo2,combo4,copy_json,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'issued',?)",
                (task_id, library_id, emp, user.get('name'), user.get('department'),
                 mine, combo['cover'], json.dumps(combo['combo2']), json.dumps(combo['combo4']),
                 json.dumps(copy_json) if copy_json is not None else None, int(time.time())))
            c.execute("COMMIT")
            return 'ok', {"pack_id": cur.lastrowid, "combo": combo, "post_index": mine}
        except sqlite3.IntegrityError:
            # 唯一索引撞车(极小概率:BEGIN IMMEDIATE 已串行化,此为兜底)→ 重选
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            continue
        except Exception as e:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            return 'error', str(e)
        finally:
            c.close()
    return 'error', '组合分配冲突重试仍失败,请稍后再试'


def set_pack_copy(pack_id, copy_json):
    """领取成功后回填文案快照(文案生成在事务外,避免 LLM 调用拖长写锁)。"""
    _ensure()
    c = _conn()
    try:
        c.execute("UPDATE kos_packs SET copy_json=? WHERE id=?",
                  (json.dumps(copy_json) if copy_json is not None else None, pack_id))
        c.commit()
        return True
    finally:
        c.close()


def delete_pack(pack_id):
    """删除一条领取记录(领取后下载/出图失败时回滚占用,组合与限额随之释放)。"""
    _ensure()
    c = _conn()
    try:
        c.execute("DELETE FROM kos_packs WHERE id=?", (pack_id,))
        c.commit()
        return True
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


# ──────────────── 本期(任务周期:周三 → 下周二)────────────────

def current_period_start_ts():
    """本期起点 = 最近一个周三的 00:00(本地时区)。周期为周三 → 下周二。"""
    import datetime
    now = datetime.datetime.now()
    days_since_wed = (now.weekday() - 2) % 7      # 周三 weekday()==2
    start = (now - datetime.timedelta(days=days_since_wed)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp())


def period_completion(org='johnson'):
    """本期任务完成情况(合并当前全部进行中任务)。

    口径(2026-07 与管理员确认):
      - 任务集合 = status='open' 的任务;每个任务的目标人群 = 在职员工
        (scope='all' 全员;scope='dept' 时限 depts 内的部门)。
      - 每人应完成 = 覆盖到 TA 的各任务 per_person 之和;
        已完成 = 每个覆盖任务都发满(published >= per_person)才算「完成」。
      - 未完成名单包含一篇都没领取的人(未开始),领了没发满为「进行中」。
    返回:
      {"tasks": [任务摘要], "people": [{name, department, emp_id,
        owed, published, issued, status: done|doing|todo}]}
    """
    _ensure()
    c = _conn()
    try:
        tasks = [dict(r) for r in c.execute(
            "SELECT id, title, brand, product, scope, depts, per_person, deadline "
            "FROM kos_tasks WHERE org=? AND status='open' ORDER BY created_at DESC", (org,)).fetchall()]
        for t in tasks:
            t['depts'] = json.loads(t.get('depts') or '[]')
        if not tasks:
            return {"tasks": [], "people": []}

        # 在职且参与 KOS 的员工名单(目标人群基数;kos_join=0 的岗位如世代主管不计入)
        try:
            users = [dict(r) for r in c.execute(
                "SELECT name, emp_id, department FROM app_users "
                "WHERE org=? AND active=1 AND COALESCE(kos_join,1)=1", (org,)).fetchall()]
        except sqlite3.OperationalError:
            # 兼容:users_store 迁移尚未执行(无 kos_join 列)时按全员统计
            users = [dict(r) for r in c.execute(
                "SELECT name, emp_id, department FROM app_users "
                "WHERE org=? AND active=1", (org,)).fetchall()]

        # 每任务每人的领取/发布数
        tids = [t['id'] for t in tasks]
        ph = ','.join('?' * len(tids))
        stat = {}   # (task_id, emp_id) -> [issued, published]
        for r in c.execute(
                f"SELECT task_id, emp_id, COUNT(*) n, "
                f"SUM(CASE WHEN status='published' THEN 1 ELSE 0 END) p "
                f"FROM kos_packs WHERE task_id IN ({ph}) GROUP BY task_id, emp_id", tids):
            stat[(r['task_id'], str(r['emp_id'] or ''))] = [r['n'], r['p'] or 0]

        people = []
        for u in users:
            emp = str(u['emp_id'] or '')
            covering = [t for t in tasks
                        if t['scope'] == 'all' or (u['department'] in (t['depts'] or []))]
            if not covering:
                continue   # 任务都没覆盖到 TA,不进名单
            owed = sum(int(t['per_person'] or 1) for t in covering)
            issued = published = 0
            all_done = True
            for t in covering:
                n, p = stat.get((t['id'], emp), (0, 0))
                issued += n
                published += p
                if p < int(t['per_person'] or 1):
                    all_done = False
            status = 'done' if all_done else ('doing' if issued > 0 else 'todo')
            people.append({"name": u['name'], "department": u['department'], "emp_id": emp,
                           "owed": owed, "published": published, "issued": issued,
                           "status": status})
        # 排序:未开始在前(需要催办)→ 进行中 → 已完成;同组内按部门
        order = {'todo': 0, 'doing': 1, 'done': 2}
        people.sort(key=lambda x: (order[x['status']], x['department'] or '', x['name'] or ''))
        return {"tasks": tasks, "people": people}
    finally:
        c.close()


# ──────────────── 自发布笔记(非任务,业务自行登记)────────────────

def add_self_post(user, note_url):
    """业务登记一条自发布的小红书笔记(自行贴链接)。链接须符合小红书规则,只存纯链接。
    返回 True / 'bad_url'。"""
    _ensure()
    url = extract_note_url(note_url)
    if not url:
        return 'bad_url'
    user = user or {}
    c = _conn()
    try:
        c.execute("INSERT INTO kos_self_posts(org,emp_id,user_name,department,note_url,created_at) "
                  "VALUES('johnson',?,?,?,?,?)",
                  (user.get('emp_id'), user.get('name'), user.get('department'), url, int(time.time())))
        c.commit()
        return True
    finally:
        c.close()


def my_self_posts(emp_id, limit=50):
    _ensure()
    c = _conn()
    try:
        return [dict(r) for r in c.execute(
            "SELECT * FROM kos_self_posts WHERE emp_id=? ORDER BY created_at DESC LIMIT ?",
            (emp_id, limit)).fetchall()]
    finally:
        c.close()


def delete_self_post(post_id, emp_id):
    _ensure()
    c = _conn()
    try:
        r = c.execute("SELECT emp_id FROM kos_self_posts WHERE id=?", (post_id,)).fetchone()
        if not r or r['emp_id'] != emp_id:
            return False
        c.execute("DELETE FROM kos_self_posts WHERE id=?", (post_id,))
        c.commit()
        return True
    finally:
        c.close()


def my_kos_summary(user):
    """顶部两张卡片的数据(本期):
      task_target  本期各在办任务(适用于该业务)每人应发篇数之和
      task_done    本期已完成(发布)的任务笔记数
      self_done    本期已登记的自发布笔记数
    """
    _ensure()
    user = user or {}
    emp = user.get('emp_id')
    dept = user.get('department')
    start = current_period_start_ts()
    c = _conn()
    try:
        target = 0
        for r in c.execute("SELECT scope, depts, per_person FROM kos_tasks "
                           "WHERE org='johnson' AND status='open'").fetchall():
            if r['scope'] == 'dept' and dept not in json.loads(r['depts'] or '[]'):
                continue
            target += max(1, int(r['per_person'] or 1))
        task_done = c.execute(
            "SELECT COUNT(*) FROM kos_packs WHERE emp_id=? AND status='published' "
            "AND COALESCE(published_at,created_at)>=?", (emp, start)).fetchone()[0]
        self_done = c.execute(
            "SELECT COUNT(*) FROM kos_self_posts WHERE emp_id=? AND created_at>=?",
            (emp, start)).fetchone()[0]
        return {"task_target": target, "task_done": task_done,
                "self_done": self_done, "period_start": start}
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


import re as _re

# 只接受小红书链接:手机端 xhslink.com 短链 / 电脑端 xiaohongshu.com。
# 小红书分享出来的是"一段口令+链接+文字",这里只把其中的链接抽出来存,避免整段被当成 URL。
_URLCH = r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%\-]"
_XHS_RE = _re.compile(r'https?://' + _URLCH + r'*?(?:xhslink\.com|xiaohongshu\.com)' + _URLCH + r'*', _re.I)


_XHS_HOSTS = ('xhslink.com', 'xiaohongshu.com')


def _is_xhs_host(url):
    """严格校验域名(2026-07 安全修复):此前只要 URL 任意位置出现
    xiaohongshu.com 字样即通过,https://evil.com/xiaohongshu.com、
    https://xhslink.com.evil.com/ 都能混进库再呈现给管理员点击(钓鱼面)。
    现在解析 hostname 精确匹配官方域及其子域。"""
    try:
        from urllib.parse import urlsplit
        host = (urlsplit(url).hostname or '').lower()
    except Exception:
        return False
    return any(host == h or host.endswith('.' + h) for h in _XHS_HOSTS)


def extract_note_url(text):
    """从粘贴的分享文案中抽出真正的小红书链接(域名严格校验);抽不到返回空串。"""
    for m in _XHS_RE.finditer(text or ''):
        url = m.group(0).strip().rstrip('.,;:!)')   # 去掉可能粘连的尾部符号
        if _is_xhs_host(url):
            return url
    return ''


def valid_note_url(url):
    """小红书链接规则校验:文本里必须含 xhslink.com 或 xiaohongshu.com 链接。"""
    return bool(extract_note_url(url))


def publish_pack(pack_id, emp_id, note_url):
    """业务回填小红书链接 → 标记已发布。仅本人可操作,且链接须符合小红书规则。
    存库时只存抽取出的纯链接。返回 True / 'not_owner' / 'bad_url'。"""
    _ensure()
    note_url = extract_note_url(note_url)
    if not note_url:
        return 'bad_url'
    c = _conn()
    try:
        r = c.execute("SELECT emp_id FROM kos_packs WHERE id=?", (pack_id,)).fetchone()
        if not r or r['emp_id'] != emp_id:
            return 'not_owner'
        c.execute("UPDATE kos_packs SET status='published', note_url=?, published_at=? WHERE id=?",
                  (note_url, int(time.time()), pack_id))
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
    """本期排行:每人 本期任务完成笔记数 + 本期自发布笔记数。按两者之和排序。"""
    _ensure()
    start = current_period_start_ts()
    c = _conn()
    try:
        agg = {}
        # 本期任务完成(已发布任务笔记)
        for r in c.execute(
                "SELECT emp_id, user_name, department, COUNT(*) c FROM kos_packs "
                "WHERE status='published' AND COALESCE(published_at,created_at)>=? "
                "GROUP BY emp_id", (start,)).fetchall():
            e = r['emp_id'] or ''
            a = agg.setdefault(e, {"emp_id": e, "name": r['user_name'], "department": r['department'],
                                   "task_notes": 0, "self_notes": 0})
            a["task_notes"] += r['c']
        # 本期自发布
        for r in c.execute(
                "SELECT emp_id, user_name, department, COUNT(*) c FROM kos_self_posts "
                "WHERE created_at>=? GROUP BY emp_id", (start,)).fetchall():
            e = r['emp_id'] or ''
            a = agg.setdefault(e, {"emp_id": e, "name": r['user_name'], "department": r['department'],
                                   "task_notes": 0, "self_notes": 0})
            a["self_notes"] += r['c']
            if not a.get("name"):
                a["name"] = r['user_name']; a["department"] = r['department']
        board = sorted(agg.values(), key=lambda x: (-(x["task_notes"] + x["self_notes"]),
                                                    -x["task_notes"]))
        return board
    finally:
        c.close()


def kos_dashboard(days=30):
    """KOS 看板聚合(时间范围内):任务/领取/发布/完成率 + 笔记排行 + 素材库用量 + 每日发布。"""
    _ensure()
    since = int(time.time() - days * 86400) if days and days > 0 else 0
    c = _conn()
    try:
        tasks = c.execute("SELECT COUNT(*) FROM kos_tasks").fetchone()[0]
        open_tasks = c.execute("SELECT COUNT(*) FROM kos_tasks WHERE status='open'").fetchone()[0]
        issued = c.execute("SELECT COUNT(*) FROM kos_packs WHERE created_at>=?", (since,)).fetchone()[0]
        published = c.execute(
            "SELECT COUNT(*) FROM kos_packs WHERE status='published' AND COALESCE(published_at,created_at)>=?",
            (since,)).fetchone()[0]
        self_published = c.execute(
            "SELECT COUNT(*) FROM kos_self_posts WHERE created_at>=?", (since,)).fetchone()[0]
        # 笔记排行 = 任务笔记 + 自发布笔记 合并计数(按 emp_id 合并)
        agg = {}
        for r in c.execute(
                "SELECT emp_id, user_name, department, COUNT(*) c FROM kos_packs "
                "WHERE status='published' AND COALESCE(published_at,created_at)>=? "
                "GROUP BY emp_id", (since,)).fetchall():
            e = r["emp_id"] or r["user_name"] or ''
            agg[e] = {"name": r["user_name"], "department": r["department"],
                      "task_count": r["c"], "self_count": 0}
        for r in c.execute(
                "SELECT emp_id, user_name, department, COUNT(*) c FROM kos_self_posts "
                "WHERE created_at>=? GROUP BY emp_id", (since,)).fetchall():
            e = r["emp_id"] or r["user_name"] or ''
            a = agg.setdefault(e, {"name": r["user_name"], "department": r["department"],
                                   "task_count": 0, "self_count": 0})
            a["self_count"] = r["c"]
            if not a.get("name"):
                a["name"] = r["user_name"]; a["department"] = r["department"]
        by_user = sorted(
            [dict(a, count=a["task_count"] + a["self_count"]) for a in agg.values()],
            key=lambda x: (-x["count"], -x["task_count"]))[:20]
        by_library = []
        for l in c.execute("SELECT id,brand,product,code FROM kos_libraries WHERE active=1 ORDER BY created_at DESC").fetchall():
            cap = capacity(l["id"])
            by_library.append({"brand": l["brand"], "product": l["product"], "code": l["code"],
                               "used": cap["used"], "total": cap["total"],
                               "mains": cap["mains"], "two": cap["two"], "four": cap["four"],
                               "mode": cap.get("mode", "legacy"),
                               "two_near": cap.get("two_near", 0), "two_far": cap.get("two_far", 0),
                               "four_near": cap.get("four_near", 0), "four_far": cap.get("four_far", 0)})
        drows = c.execute(
            "SELECT strftime('%Y-%m-%d', COALESCE(published_at,created_at),'unixepoch','localtime') d, COUNT(*) c "
            "FROM kos_packs WHERE status='published' AND COALESCE(published_at,created_at)>=? "
            "GROUP BY d ORDER BY d DESC LIMIT 30", (since,)).fetchall()
        by_daily = [{"key": r["d"], "count": r["c"]} for r in drows]
        # 任务笔记链接:已发布且有链接的记录,管理员统一点开查看
        nrows = c.execute(
            "SELECT p.user_name, p.department, p.note_url, p.published_at, "
            "       t.title, t.brand, t.product "
            "FROM kos_packs p LEFT JOIN kos_tasks t ON p.task_id=t.id "
            "WHERE p.status='published' AND p.note_url IS NOT NULL AND p.note_url<>'' "
            "AND COALESCE(p.published_at,p.created_at)>=? "
            "ORDER BY COALESCE(p.published_at,p.created_at) DESC LIMIT 300", (since,)).fetchall()
        task_notes = [{
            "name": r["user_name"], "department": r["department"],
            "url": extract_note_url(r["note_url"]) or r["note_url"],   # 兼容历史脏数据,展示纯链接
            "at": r["published_at"],
            "task": (r["title"] or ((r["brand"] or '') + ' ' + (r["product"] or ''))).strip(),
        } for r in nrows]
        # 自发布笔记链接:业务自发的非任务笔记,同样给管理员点开查看
        srows = c.execute(
            "SELECT user_name, department, note_url, created_at FROM kos_self_posts "
            "WHERE created_at>=? ORDER BY created_at DESC LIMIT 300", (since,)).fetchall()
        self_notes = [{
            "name": r["user_name"], "department": r["department"],
            "url": extract_note_url(r["note_url"]) or r["note_url"],
            "at": r["created_at"],
        } for r in srows]
        return {"tasks": tasks, "open_tasks": open_tasks, "issued": issued, "published": published,
                "completion_pct": (round(published / issued * 100) if issued else 0),
                "self_published": self_published,
                "by_user": by_user, "by_library": by_library, "by_daily": by_daily,
                "task_notes": task_notes, "self_notes": self_notes,
                "completion": period_completion()}
    finally:
        c.close()
