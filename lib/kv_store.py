"""本地 SQLite 用量日志存储。

此前用 Upstash(Vercel KV)走 HTTPS,现已切换到本机 SQLite。
数据库文件默认放在 <项目根>/data/usage.db,可通过环境变量 USAGE_DB_PATH 覆盖。

API 签名跟旧版 Upstash 实现完全一致:
  - log_event(action, user, details)   写入
  - get_recent_logs(n)                 取最近事件
  - get_stats()                        汇总各维度
  - _kv_available()                    是否可用(SQLite 几乎总是可用)

设计要点:
  • 单表 events,所有事件追加写入,统计用 SQL GROUP BY 聚合
    (好处:不用维护一堆 counter,SQL 支持任意时间范围筛选)
  • WAL 模式开启,允许并发读 + 单写,适合 systemd 服务多线程场景
  • 写入失败静默(打日志),不影响主业务流程
"""
import os
import json
import time
import datetime
import sqlite3
import threading
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get('USAGE_DB_PATH', str(_ROOT / 'data' / 'usage.db')))

_db_lock = threading.Lock()       # 写操作串行,避免 SQLite "database is locked"
_schema_initialized = False


# 产品名别名 → 归一化名(用于早期测试遗留的产品名归并)
# key 是 details_json 里曾经出现过的"别名",value 是 products.json 里的规范名
_PRODUCT_ALIASES = {
    'TX-5智能跑步机': '智能跑步机TX-5',
    'TX3跑步机':      '智能跑步机TX3',
    # 如果以后又出现新的命名混乱,在这里加一行即可,不需要改 SQL
}


def _canon_product(name):
    """把产品名归一化为 products.json 里的规范名。未命中别名表的原样返回。"""
    if not name:
        return name
    return _PRODUCT_ALIASES.get(name, name)


def _get_conn():
    """统一走 lib/db 的线程级复用连接(元组行,保持原有 row[0] 访问方式)"""
    from lib.db import get_conn
    return get_conn(row_factory=None)


def _init_schema():
    """幂等创建 events 表 + 索引"""
    global _schema_initialized
    if _schema_initialized:
        return
    with _db_lock:
        if _schema_initialized:
            return
        conn = _get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    time_ms       INTEGER NOT NULL,
                    action        TEXT NOT NULL,
                    emp_id        TEXT,
                    user_name     TEXT,
                    department    TEXT,
                    details_json  TEXT,
                    day           TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_day    ON events(day);
                CREATE INDEX IF NOT EXISTS idx_events_action ON events(action);
                CREATE INDEX IF NOT EXISTS idx_events_emp    ON events(emp_id);
                CREATE INDEX IF NOT EXISTS idx_events_time   ON events(time_ms DESC);

                -- 售前问答用的产品资料库(OCR 入库)
                CREATE TABLE IF NOT EXISTS manuals (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    brand        TEXT NOT NULL,
                    product      TEXT NOT NULL,
                    source_type  TEXT NOT NULL,
                    source_file  TEXT NOT NULL,
                    page_no      INTEGER,
                    content      TEXT NOT NULL,
                    char_count   INTEGER NOT NULL,
                    created_at   INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_manuals_product ON manuals(brand, product);
                CREATE INDEX IF NOT EXISTS idx_manuals_source  ON manuals(source_file);

                CREATE TABLE IF NOT EXISTS manuals_files (
                    file_path     TEXT PRIMARY KEY,
                    file_hash     TEXT NOT NULL,
                    brand         TEXT NOT NULL,
                    product       TEXT NOT NULL,
                    source_type   TEXT NOT NULL,
                    total_pages   INTEGER,
                    total_chars   INTEGER,
                    cost_yuan     REAL,
                    status        TEXT NOT NULL,
                    error         TEXT,
                    completed_at  INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_files_hash ON manuals_files(file_hash);
            """)
            conn.commit()
            _schema_initialized = True
        finally:
            conn.close()


# 模块导入时建表
try:
    _init_schema()
except Exception as e:
    print(f"[kv_store] 初始化失败:{e}", flush=True)


def _kv_available() -> bool:
    """SQLite 几乎总是可用 — 只要文件能创建。
    保留这个函数名是为了不动 /api/admin-stats.py 等调用方。"""
    try:
        return DB_PATH.parent.exists() or DB_PATH.parent.parent.exists()
    except Exception:
        return False


def log_event(action: str, user: dict, details: dict = None):
    """记录一次用量事件。

    action:  'login' / 'generate' / 'cover_fields' / 'cover_generate'
    user:    {'emp_id', 'name', 'department'}
    details: 自由附加信息,如 {'brand': 'XX', 'style': 'XX', 'copy_type': 'XX'}

    失败静默 — 不抛异常、不阻塞业务请求。
    """
    try:
        u = user or {}
        emp_id = (u.get('emp_id') or 'unknown').strip() or 'unknown'
        name = (u.get('name') or '').strip()
        dept = (u.get('department') or 'unknown').strip() or 'unknown'
        details_json = json.dumps(details or {}, ensure_ascii=False)
        now = datetime.datetime.now()
        time_ms = int(now.timestamp() * 1000)
        day = now.strftime('%Y-%m-%d')

        with _db_lock:
            conn = _get_conn()
            try:
                conn.execute(
                    "INSERT INTO events (time_ms, action, emp_id, user_name, "
                    "department, details_json, day) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (time_ms, action, emp_id, name, dept, details_json, day)
                )
                conn.commit()
            finally:
                conn.close()
    except Exception as e:
        print(f"[kv_store] log_event 失败:{e}", flush=True)


def get_recent_logs(n: int = 100, action_filter=None):
    """取最近 N 条事件(新→旧),可按 action 列表过滤"""
    try:
        conn = _get_conn()
        try:
            if action_filter:
                placeholders = ','.join(['?'] * len(action_filter))
                sql = (
                    "SELECT time_ms, action, emp_id, user_name, department, details_json "
                    f"FROM events WHERE action IN ({placeholders}) "
                    "ORDER BY time_ms DESC LIMIT ?"
                )
                params = tuple(action_filter) + (max(1, min(n, 1000)),)
            else:
                sql = (
                    "SELECT time_ms, action, emp_id, user_name, department, details_json "
                    "FROM events ORDER BY time_ms DESC LIMIT ?"
                )
                params = (max(1, min(n, 1000)),)
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            details = json.loads(r[5]) if r[5] else {}
            # 明细表里的产品名也要归一化,否则会跟聚合维度对不上
            if details.get('product'):
                details['product'] = _canon_product(details['product'])
            out.append({
                'time': r[0],
                'action': r[1],
                'user': {'emp_id': r[2], 'name': r[3], 'department': r[4]},
                'details': details,
            })
        return out
    except Exception as e:
        print(f"[kv_store] get_recent_logs 失败:{e}", flush=True)
        return []


def get_counter(key: str) -> int:
    """兼容旧 API — 但 SQLite 改用聚合查询,本函数已不被内部使用。
    保留是为了向后兼容外部调用(如果有的话)。"""
    return 0


def get_events_page(action_filter=None, keyword='', days=30, page=0, page_size=50):
    """分页 + 关键词搜索的事件明细。keyword 匹配姓名/工号/详情。"""
    try:
        conds, params = [], []
        if action_filter:
            ph = ','.join(['?'] * len(action_filter))
            conds.append(f"action IN ({ph})")
            params += list(action_filter)
        if days and days > 0:
            since = int((datetime.datetime.now().timestamp() - days * 86400) * 1000)
            conds.append("time_ms>=?")
            params.append(since)
        kw = (keyword or '').strip()
        if kw:
            conds.append("(user_name LIKE ? OR emp_id LIKE ? OR details_json LIKE ?)")
            like = f"%{kw}%"
            params += [like, like, like]
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        page = max(0, int(page))
        ps = max(1, min(int(page_size), 200))
        conn = _get_conn()
        try:
            total = conn.execute(f"SELECT COUNT(*) FROM events{where}", tuple(params)).fetchone()[0]
            rows = conn.execute(
                f"SELECT time_ms,action,emp_id,user_name,department,details_json FROM events{where} "
                "ORDER BY time_ms DESC LIMIT ? OFFSET ?", tuple(params) + (ps, page * ps)).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            details = json.loads(r[5]) if r[5] else {}
            if details.get('product'):
                details['product'] = _canon_product(details['product'])
            out.append({'time': r[0], 'action': r[1],
                        'user': {'emp_id': r[2], 'name': r[3], 'department': r[4]}, 'details': details})
        return {'events': out, 'total': total, 'page': page,
                'pages': (total + ps - 1) // ps, 'page_size': ps}
    except Exception as e:
        print(f"[kv_store] get_events_page 失败:{e}", flush=True)
        return {'events': [], 'total': 0, 'page': 0, 'pages': 0, 'page_size': page_size}


def get_overview(days=30):
    """平台总览:活跃人数(去重)+ 各模块用量 + 每日趋势 + 最活跃的人。"""
    try:
        now = datetime.datetime.now()
        today = now.strftime('%Y-%m-%d')
        week_since = int((now.timestamp() - 7 * 86400) * 1000)
        range_since = int((now.timestamp() - days * 86400) * 1000) if days and days > 0 else 0
        real_user = "emp_id IS NOT NULL AND emp_id NOT IN ('unknown','')"
        conn = _get_conn()
        try:
            def scalar(sql, p=()):
                return conn.execute(sql, p).fetchone()[0]

            active_today = scalar(
                f"SELECT COUNT(DISTINCT emp_id) FROM events WHERE day=? AND {real_user}", (today,))
            active_week = scalar(
                f"SELECT COUNT(DISTINCT emp_id) FROM events WHERE time_ms>=? AND {real_user}", (week_since,))
            active_range = scalar(
                f"SELECT COUNT(DISTINCT emp_id) FROM events WHERE time_ms>=? AND {real_user}", (range_since,))

            def cnt(actions):
                ph = ','.join(['?'] * len(actions))
                return scalar(f"SELECT COUNT(*) FROM events WHERE action IN ({ph}) AND time_ms>=?",
                              tuple(actions) + (range_since,))

            modules = {
                "content": cnt(['generate', 'cover_generate', 'cover_fields']),
                "qa": cnt(['qa']),
                "library": cnt(['download']),
            }

            by_daily = [{'key': r[0], 'count': r[1]} for r in conn.execute(
                "SELECT day, COUNT(*) FROM events WHERE time_ms>=? GROUP BY day ORDER BY day DESC LIMIT 45",
                (range_since,)).fetchall()]
            by_daily_active = [{'key': r[0], 'count': r[1]} for r in conn.execute(
                f"SELECT day, COUNT(DISTINCT emp_id) FROM events WHERE time_ms>=? AND {real_user} "
                "GROUP BY day ORDER BY day DESC LIMIT 45", (range_since,)).fetchall()]
            top_users_raw = [{'key': r[0], 'count': r[1]} for r in conn.execute(
                f"SELECT emp_id, COUNT(*) c FROM events WHERE time_ms>=? AND {real_user} "
                "GROUP BY emp_id ORDER BY c DESC LIMIT 20", (range_since,)).fetchall()]
        finally:
            conn.close()
        return {
            "active_today": active_today, "active_week": active_week, "active_range": active_range,
            "modules": modules, "by_daily": by_daily, "by_daily_active": by_daily_active,
            "top_users_raw": top_users_raw,
        }
    except Exception as e:
        print(f"[kv_store] get_overview 失败:{e}", flush=True)
        return {"active_today": 0, "active_week": 0, "active_range": 0, "modules": {},
                "by_daily": [], "by_daily_active": [], "top_users_raw": []}


def get_stats(action_filter=None, days=30):
    """聚合所有维度数据,供 /api/admin-stats 返回。
    action_filter: 可选,只统计这些 action 的事件;None = 全部。
    days: 时间范围(天),<=0 表示全部历史。
    """
    try:
        # 动作过滤 + 时间范围,合并成统一 WHERE 片段
        conds, params = [], []
        if action_filter:
            placeholders = ','.join(['?'] * len(action_filter))
            conds.append(f"action IN ({placeholders})")
            params += list(action_filter)
        if days and days > 0:
            since_ms = int((datetime.datetime.now().timestamp() - days * 86400) * 1000)
            conds.append("time_ms >= ?")
            params.append(since_ms)
        base_where = (" WHERE " + " AND ".join(conds) + " ") if conds else ""
        and_where = (" AND " + " AND ".join(conds) + " ") if conds else ""
        base_params = tuple(params)

        conn = _get_conn()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) FROM events {base_where}", base_params
            ).fetchone()[0]

            # 按员工 ID 分组
            by_user_raw = [
                {'key': r[0] or 'unknown', 'count': r[1]}
                for r in conn.execute(
                    f"SELECT emp_id, COUNT(*) FROM events {base_where} GROUP BY emp_id",
                    base_params,
                ).fetchall()
            ]

            # 按部门
            by_dept = sorted([
                {'key': r[0] or 'unknown', 'count': r[1]}
                for r in conn.execute(
                    f"SELECT department, COUNT(*) FROM events {base_where} GROUP BY department",
                    base_params,
                ).fetchall()
            ], key=lambda x: -x['count'])

            # 按动作(filter 后只剩相关动作)
            by_action = sorted([
                {'key': r[0], 'count': r[1]}
                for r in conn.execute(
                    f"SELECT action, COUNT(*) FROM events {base_where} GROUP BY action",
                    base_params,
                ).fetchall()
            ], key=lambda x: -x['count'])

            # 按天(最近 30 天)
            by_daily = [
                {'key': r[0], 'count': r[1]}
                for r in conn.execute(
                    f"SELECT day, COUNT(*) FROM events {base_where} "
                    "GROUP BY day ORDER BY day DESC LIMIT 30",
                    base_params,
                ).fetchall()
            ]

            # 从 details_json 抽取 style / brand 字段做分组
            by_style = sorted([
                {'key': r[0], 'count': r[1]}
                for r in conn.execute(
                    "SELECT json_extract(details_json, '$.style') AS s, COUNT(*) "
                    f"FROM events WHERE s IS NOT NULL AND s != '' {and_where} "
                    "GROUP BY s",
                    base_params,
                ).fetchall()
            ], key=lambda x: -x['count'])

            by_brand = sorted([
                {'key': r[0], 'count': r[1]}
                for r in conn.execute(
                    "SELECT json_extract(details_json, '$.brand') AS b, COUNT(*) "
                    f"FROM events WHERE b IS NOT NULL AND b != '' {and_where} "
                    "GROUP BY b",
                    base_params,
                ).fetchall()
            ], key=lambda x: -x['count'])

            # 产品维度(从 details_json 抽取)+ 别名归一化合并
            raw_products = conn.execute(
                "SELECT json_extract(details_json, '$.product') AS p, COUNT(*) "
                f"FROM events WHERE p IS NOT NULL AND p != '' {and_where} "
                "GROUP BY p",
                base_params,
            ).fetchall()
            _merged = {}
            for name, count in raw_products:
                canon = _canon_product(name)
                _merged[canon] = _merged.get(canon, 0) + count
            by_product = sorted(
                [{'key': k, 'count': v} for k, v in _merged.items()],
                key=lambda x: -x['count']
            )

            # 文案类型维度(种草/场景/促销/干货)
            by_copy_type = sorted([
                {'key': r[0], 'count': r[1]}
                for r in conn.execute(
                    "SELECT json_extract(details_json, '$.copy_type') AS c, COUNT(*) "
                    f"FROM events WHERE c IS NOT NULL AND c != '' {and_where} "
                    "GROUP BY c",
                    base_params,
                ).fetchall()
            ], key=lambda x: -x['count'])

            # Top 问题(只对 qa 应用有意义,其它情况会返回空列表)
            # 取 details_json.question 文本,按完全一致聚合,Top 30
            by_question = [
                {'key': r[0], 'count': r[1]}
                for r in conn.execute(
                    "SELECT json_extract(details_json, '$.question') AS q, COUNT(*) c "
                    f"FROM events WHERE q IS NOT NULL AND q != '' {and_where} "
                    "GROUP BY q ORDER BY c DESC LIMIT 30",
                    base_params,
                ).fetchall()
            ]

            # 小时分布(0-23)用于热力图
            by_hour = [
                {'key': r[0], 'count': r[1]}
                for r in conn.execute(
                    "SELECT CAST(strftime('%H', time_ms/1000, 'unixepoch', 'localtime') AS INTEGER) AS h, "
                    f"COUNT(*) FROM events {base_where} GROUP BY h ORDER BY h",
                    base_params,
                ).fetchall()
            ]

        finally:
            conn.close()

        return {
            'total': total,
            'by_user_raw': by_user_raw,
            'by_dept': by_dept,
            'by_action': by_action,
            'by_style': by_style,
            'by_brand': by_brand,
            'by_product': by_product,
            'by_copy_type': by_copy_type,
            'by_hour': by_hour,
            'by_daily': by_daily,
            'by_question': by_question,
            'recent': get_recent_logs(200, action_filter=action_filter),
        }
    except Exception as e:
        print(f"[kv_store] get_stats 失败:{e}", flush=True)
        return None
