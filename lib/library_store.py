"""产品资料库文件的数据存储(usage.db 的 library_files 表)。

把原本静态的 data/library_manifest.json 收进数据库,支持后台上传/删除/改名。
  - cos_key 为相对「产品库」根的路径;实际下载 = COS_PREFIX + cos_key(见 api/library.py)。
  - name 是展示/下载文件名(可改),与 cos_key(实际存储)解耦。
兜底:数据库不可用 / 为空且播种失败时,读函数回退直接读 manifest,保证下载不挂。
"""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get('USAGE_DB_PATH', str(_ROOT / 'data' / 'usage.db')))
MANIFEST = _ROOT / 'data' / 'library_manifest.json'

_lock = threading.Lock()
_seeded = False


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    c.execute('PRAGMA journal_mode=WAL')
    c.row_factory = sqlite3.Row
    return c


def _ensure_schema(c):
    c.executescript("""
        CREATE TABLE IF NOT EXISTS library_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            org TEXT NOT NULL DEFAULT 'johnson',
            brand TEXT NOT NULL, product TEXT NOT NULL,
            name TEXT NOT NULL, ftype TEXT,
            cos_key TEXT NOT NULL,
            size INTEGER DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER, updated_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_libf_prod ON library_files(brand, product);
        CREATE INDEX IF NOT EXISTS idx_libf_key ON library_files(cos_key);
    """)


def _load_manifest():
    try:
        return json.loads(MANIFEST.read_text(encoding='utf-8'))
    except Exception:
        return {"brands": {}}


def _ensure_seeded():
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
                n = c.execute("SELECT COUNT(*) FROM library_files").fetchone()[0]
                if n == 0:
                    now = int(time.time())
                    order = 0
                    brands = _load_manifest().get("brands", {})
                    for brand, products in brands.items():
                        for p in products:
                            for f in p.get("files", []):
                                c.execute(
                                    "INSERT INTO library_files(org,brand,product,name,ftype,cos_key,size,"
                                    "sort_order,active,created_at,updated_at) VALUES('johnson',?,?,?,?,?,?,?,1,?,?)",
                                    (brand, p["name"], f["name"], f.get("type", ""), f["key"],
                                     f.get("size", 0), order, now, now))
                                order += 1
                    c.commit()
                _seeded = True
            finally:
                c.close()
        except Exception:
            _seeded = True


# ──────────────── 读 ────────────────

def grouped():
    """给 /api/library GET:{brands: {品牌: [{name, files:[{name,type,key,size}]}]}}。"""
    _ensure_seeded()
    try:
        c = _conn()
        try:
            rows = c.execute("SELECT * FROM library_files WHERE active=1 ORDER BY sort_order, id").fetchall()
            if rows:
                brands = {}
                for r in rows:
                    b = brands.setdefault(r["brand"], {})
                    prod = b.setdefault(r["product"], [])
                    prod.append({"name": r["name"], "type": r["ftype"] or "", "key": r["cos_key"], "size": r["size"]})
                out = {}
                for b, prods in brands.items():
                    out[b] = [{"name": pn, "files": fs} for pn, fs in prods.items()]
                return out
        finally:
            c.close()
    except Exception:
        pass
    return _load_manifest().get("brands", {})


def allowed_keys():
    """下载白名单:{cos_key: {brand, product, name, type}}。"""
    _ensure_seeded()
    out = {}
    try:
        c = _conn()
        try:
            rows = c.execute("SELECT brand,product,name,ftype,cos_key FROM library_files WHERE active=1").fetchall()
            if rows:
                for r in rows:
                    out[r["cos_key"]] = {"brand": r["brand"], "product": r["product"],
                                         "name": r["name"], "type": r["ftype"] or ""}
                return out
        finally:
            c.close()
    except Exception:
        pass
    for brand, products in _load_manifest().get("brands", {}).items():
        for p in products:
            for f in p.get("files", []):
                out[f["key"]] = {"brand": brand, "product": p["name"], "name": f["name"], "type": f.get("type", "")}
    return out


def list_by_product(brand, product):
    """后台管理:某产品的资料文件(含 id)。"""
    _ensure_seeded()
    try:
        c = _conn()
        try:
            rows = c.execute(
                "SELECT id,name,ftype,cos_key,size FROM library_files WHERE active=1 AND brand=? AND product=? "
                "ORDER BY sort_order, id", (brand, product)).fetchall()
            return [{"id": r["id"], "name": r["name"], "type": r["ftype"] or "", "key": r["cos_key"], "size": r["size"]}
                    for r in rows]
        finally:
            c.close()
    except Exception:
        return []


def key_exists(cos_key):
    _ensure_seeded()
    try:
        c = _conn()
        try:
            return c.execute("SELECT 1 FROM library_files WHERE cos_key=? AND active=1 LIMIT 1", (cos_key,)).fetchone() is not None
        finally:
            c.close()
    except Exception:
        return False


# ──────────────── 写 ────────────────

def add_file(brand, product, name, ftype, cos_key, size=0):
    _ensure_seeded()
    now = int(time.time())
    c = _conn()
    try:
        mx = c.execute("SELECT COALESCE(MAX(sort_order),0) FROM library_files WHERE brand=? AND product=?",
                       (brand, product)).fetchone()[0]
        cur = c.execute(
            "INSERT INTO library_files(org,brand,product,name,ftype,cos_key,size,sort_order,active,created_at,updated_at) "
            "VALUES('johnson',?,?,?,?,?,?,?,1,?,?)",
            (brand, product, name.strip(), ftype, cos_key.strip(), size, mx + 1, now, now))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def update_file(fid, name=None, ftype=None):
    _ensure_seeded()
    sets, vals = [], []
    if name is not None:
        sets.append("name=?"); vals.append(name.strip())
    if ftype is not None:
        sets.append("ftype=?"); vals.append(ftype)
    if not sets:
        return False
    sets.append("updated_at=?"); vals.append(int(time.time()))
    vals.append(fid)
    c = _conn()
    try:
        c.execute(f"UPDATE library_files SET {', '.join(sets)} WHERE id=?", vals)
        c.commit()
        return True
    finally:
        c.close()


def deactivate_file(fid):
    _ensure_seeded()
    c = _conn()
    try:
        c.execute("UPDATE library_files SET active=0 WHERE id=?", (fid,))
        c.commit()
        return True
    finally:
        c.close()
