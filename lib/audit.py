"""管理员操作审计日志(usage.db 的 admin_audit 表)。

记录所有管理员在后台的写操作(用户 / 产品 / 任务),保留近 30 天。
写入 best-effort:出错也不影响主操作。查询时自动清理超期记录。
"""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get('USAGE_DB_PATH', str(_ROOT / 'data' / 'usage.db')))
RETENTION_DAYS = 30

_lock = threading.Lock()
_ready = False


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    c.execute('PRAGMA journal_mode=WAL')
    c.row_factory = sqlite3.Row
    return c


def _ensure(c):
    global _ready
    if _ready:
        return
    c.executescript("""
        CREATE TABLE IF NOT EXISTS admin_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time_ms INTEGER NOT NULL,
            emp_id TEXT, name TEXT, department TEXT, role TEXT,
            category TEXT, action TEXT, summary TEXT,
            detail_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_audit_time ON admin_audit(time_ms DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_cat  ON admin_audit(category);
    """)
    _ready = True


def log(user, category, action, summary, detail=None):
    """记一条审计。user 是操作者 _user;category 用户/产品/任务;summary 是可读摘要。"""
    try:
        role = ''
        try:
            from lib.auth import role_of
            role = role_of(user or {})
        except Exception:
            pass
        u = user or {}
        with _lock:
            c = _conn()
            try:
                _ensure(c)
                c.execute(
                    "INSERT INTO admin_audit(time_ms,emp_id,name,department,role,category,action,summary,detail_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (int(time.time() * 1000), u.get('emp_id'), u.get('name'), u.get('department'),
                     role, category, action, summary,
                     json.dumps(detail, ensure_ascii=False) if detail is not None else None))
                c.commit()
            finally:
                c.close()
    except Exception as e:
        print(f"[audit] 记录失败:{e}", flush=True)


def recent(days=RETENTION_DAYS, category=None, limit=800):
    """返回近 days 天的审计记录(倒序),并顺手清理超期记录。"""
    try:
        with _lock:
            c = _conn()
            try:
                _ensure(c)
                cutoff = int((time.time() - RETENTION_DAYS * 86400) * 1000)
                c.execute("DELETE FROM admin_audit WHERE time_ms < ?", (cutoff,))
                c.commit()
                since = int((time.time() - days * 86400) * 1000)
                q = "SELECT * FROM admin_audit WHERE time_ms >= ?"
                args = [since]
                if category and category != '全部':
                    q += " AND category = ?"
                    args.append(category)
                q += " ORDER BY time_ms DESC LIMIT ?"
                args.append(limit)
                rows = c.execute(q, args).fetchall()
                return [dict(r) for r in rows]
            finally:
                c.close()
    except Exception:
        return []
