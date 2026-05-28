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


def _get_conn():
    """打开一个 SQLite 连接(默认开 WAL 模式 + 行级超时)"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL')      # 并发读写更稳
    conn.execute('PRAGMA synchronous=NORMAL')    # 折衷点:持久化 vs 性能
    return conn


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


def get_recent_logs(n: int = 100):
    """取最近 N 条事件(新→旧)"""
    try:
        conn = _get_conn()
        try:
            rows = conn.execute(
                "SELECT time_ms, action, emp_id, user_name, department, details_json "
                "FROM events ORDER BY time_ms DESC LIMIT ?",
                (max(1, min(n, 1000)),)
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                'time': r[0],
                'action': r[1],
                'user': {'emp_id': r[2], 'name': r[3], 'department': r[4]},
                'details': json.loads(r[5]) if r[5] else {},
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[kv_store] get_recent_logs 失败:{e}", flush=True)
        return []


def get_counter(key: str) -> int:
    """兼容旧 API — 但 SQLite 改用聚合查询,本函数已不被内部使用。
    保留是为了向后兼容外部调用(如果有的话)。"""
    return 0


def get_stats():
    """聚合所有维度数据,供 /api/admin-stats 返回。

    返回结构跟旧 Upstash 实现完全一致,前端不用改。
    """
    try:
        conn = _get_conn()
        try:
            total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

            # 按员工 ID 分组
            by_user_raw = [
                {'key': r[0] or 'unknown', 'count': r[1]}
                for r in conn.execute(
                    "SELECT emp_id, COUNT(*) FROM events GROUP BY emp_id"
                ).fetchall()
            ]

            # 按部门
            by_dept = sorted([
                {'key': r[0] or 'unknown', 'count': r[1]}
                for r in conn.execute(
                    "SELECT department, COUNT(*) FROM events GROUP BY department"
                ).fetchall()
            ], key=lambda x: -x['count'])

            # 按动作
            by_action = sorted([
                {'key': r[0], 'count': r[1]}
                for r in conn.execute(
                    "SELECT action, COUNT(*) FROM events GROUP BY action"
                ).fetchall()
            ], key=lambda x: -x['count'])

            # 按天(最近 30 天)
            by_daily = [
                {'key': r[0], 'count': r[1]}
                for r in conn.execute(
                    "SELECT day, COUNT(*) FROM events "
                    "GROUP BY day ORDER BY day DESC LIMIT 30"
                ).fetchall()
            ]

            # 从 details_json 抽取 style / brand 字段做分组
            by_style = sorted([
                {'key': r[0], 'count': r[1]}
                for r in conn.execute(
                    "SELECT json_extract(details_json, '$.style') AS s, COUNT(*) "
                    "FROM events WHERE s IS NOT NULL AND s != '' "
                    "GROUP BY s"
                ).fetchall()
            ], key=lambda x: -x['count'])

            by_brand = sorted([
                {'key': r[0], 'count': r[1]}
                for r in conn.execute(
                    "SELECT json_extract(details_json, '$.brand') AS b, COUNT(*) "
                    "FROM events WHERE b IS NOT NULL AND b != '' "
                    "GROUP BY b"
                ).fetchall()
            ], key=lambda x: -x['count'])

            # 产品维度(从 details_json 抽取)
            by_product = sorted([
                {'key': r[0], 'count': r[1]}
                for r in conn.execute(
                    "SELECT json_extract(details_json, '$.product') AS p, COUNT(*) "
                    "FROM events WHERE p IS NOT NULL AND p != '' "
                    "GROUP BY p"
                ).fetchall()
            ], key=lambda x: -x['count'])

            # 文案类型维度(种草/场景/促销/干货)
            by_copy_type = sorted([
                {'key': r[0], 'count': r[1]}
                for r in conn.execute(
                    "SELECT json_extract(details_json, '$.copy_type') AS c, COUNT(*) "
                    "FROM events WHERE c IS NOT NULL AND c != '' "
                    "GROUP BY c"
                ).fetchall()
            ], key=lambda x: -x['count'])

            # 小时分布(0-23)用于热力图
            by_hour = [
                {'key': r[0], 'count': r[1]}
                for r in conn.execute(
                    "SELECT CAST(strftime('%H', time_ms/1000, 'unixepoch', 'localtime') AS INTEGER) AS h, "
                    "COUNT(*) FROM events GROUP BY h ORDER BY h"
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
            'recent': get_recent_logs(200),
        }
    except Exception as e:
        print(f"[kv_store] get_stats 失败:{e}", flush=True)
        return None
