"""产品数据存储 —— 把原本由 build_products.py 生成、需手动维护的 data/products.json
收进数据库(usage.db 的 app_brands / app_products 表),后台可视化管理。

策略同 users_store:首次访问空表则从 products.json 播种;之后以库为准。
兜底:数据库不可用 / 为空且播种失败时,读函数回退直接读 products.json,
      保证文案 / 封面 / 下拉等功能不挂。
"""
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get('USAGE_DB_PATH', str(_ROOT / 'data' / 'usage.db')))
PRODUCTS_JSON = _ROOT / 'data' / 'products.json'

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
        CREATE TABLE IF NOT EXISTS app_brands (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            org         TEXT NOT NULL DEFAULT 'johnson',
            name        TEXT NOT NULL,
            guidelines  TEXT,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  INTEGER, updated_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS app_products (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            org         TEXT NOT NULL DEFAULT 'johnson',
            brand       TEXT NOT NULL,
            name        TEXT NOT NULL,
            content     TEXT,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            active      INTEGER NOT NULL DEFAULT 1,
            created_at  INTEGER, updated_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_prod_brand ON app_products(org, brand);
    """)


def _load_json():
    try:
        return json.loads(PRODUCTS_JSON.read_text(encoding='utf-8'))
    except Exception:
        return {"brands": []}


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
                n = c.execute("SELECT COUNT(*) FROM app_brands").fetchone()[0]
                if n == 0:
                    now = int(time.time())
                    for bi, b in enumerate(_load_json().get('brands', [])):
                        c.execute("INSERT INTO app_brands(org,name,guidelines,sort_order,active,created_at,updated_at) "
                                  "VALUES('johnson',?,?,?,1,?,?)",
                                  (b.get('name', ''), b.get('guidelines', ''), bi, now, now))
                        for pi, p in enumerate(b.get('products', [])):
                            c.execute("INSERT INTO app_products(org,brand,name,content,sort_order,active,created_at,updated_at) "
                                      "VALUES('johnson',?,?,?,?,1,?,?)",
                                      (b.get('name', ''), p.get('name', ''), p.get('content', ''), pi, now, now))
                    c.commit()
                _seeded = True
            finally:
                c.close()
        except Exception:
            _seeded = True  # 读函数回退 JSON


# ──────────────── 读 ────────────────

def list_brands_products():
    """给前端下拉:[{name, products:[{name}]}],不含全文。"""
    _ensure_seeded()
    try:
        c = _conn()
        try:
            brands = c.execute("SELECT name FROM app_brands WHERE active=1 ORDER BY sort_order").fetchall()
            if brands:
                out = []
                for b in brands:
                    prods = c.execute(
                        "SELECT name FROM app_products WHERE active=1 AND brand=? ORDER BY sort_order",
                        (b["name"],)).fetchall()
                    out.append({"name": b["name"], "products": [{"name": p["name"]} for p in prods]})
                return out
        finally:
            c.close()
    except Exception:
        pass
    return [{"name": b["name"], "products": [{"name": p["name"]} for p in b.get("products", [])]}
            for b in _load_json().get("brands", [])]


def find_product(brand_name, product_name):
    """返回 (brand_dict, product_dict),与原 find_product 兼容:
    brand_dict={'name','guidelines'};product_dict={'id','name','content'}。找不到 (None,None)。"""
    _ensure_seeded()
    bn = (brand_name or '').strip()
    pn = (product_name or '').strip()
    try:
        c = _conn()
        try:
            b = c.execute("SELECT * FROM app_brands WHERE name=? AND active=1 LIMIT 1", (bn,)).fetchone()
            p = c.execute("SELECT * FROM app_products WHERE brand=? AND name=? AND active=1 LIMIT 1",
                          (bn, pn)).fetchone()
            if p is not None:
                brand_dict = {"name": bn, "guidelines": (b["guidelines"] if b else "")}
                product_dict = {"id": p["id"], "name": p["name"], "content": p["content"] or ""}
                return brand_dict, product_dict
            # DB 里没有 → 落到 JSON 兜底
        finally:
            c.close()
    except Exception:
        pass
    for b in _load_json().get("brands", []):
        if b.get("name") == bn:
            for p in b.get("products", []):
                if p.get("name") == pn:
                    return {"name": bn, "guidelines": b.get("guidelines", "")}, \
                           {"id": None, "name": p.get("name"), "content": p.get("content", "")}
    return None, None


def get_all():
    """后台管理用:品牌(含 guidelines)+ 其产品(含 id/content)。"""
    _ensure_seeded()
    try:
        c = _conn()
        try:
            brands = c.execute("SELECT * FROM app_brands WHERE active=1 ORDER BY sort_order").fetchall()
            if brands:
                out = []
                for b in brands:
                    prods = c.execute(
                        "SELECT id,name,content FROM app_products WHERE active=1 AND brand=? ORDER BY sort_order",
                        (b["name"],)).fetchall()
                    out.append({
                        "id": b["id"], "name": b["name"], "guidelines": b["guidelines"] or "",
                        "products": [{"id": p["id"], "name": p["name"], "content": p["content"] or ""} for p in prods],
                    })
                return out
        finally:
            c.close()
    except Exception:
        pass
    return [{"id": None, "name": b.get("name"), "guidelines": b.get("guidelines", ""),
             "products": [{"id": None, "name": p.get("name"), "content": p.get("content", "")} for p in b.get("products", [])]}
            for b in _load_json().get("brands", [])]


# ──────────────── 写(后台 UI 用)────────────────

def add_product(brand, name, content=''):
    _ensure_seeded()
    now = int(time.time())
    c = _conn()
    try:
        mx = c.execute("SELECT COALESCE(MAX(sort_order),0) FROM app_products WHERE brand=?", (brand,)).fetchone()[0]
        cur = c.execute("INSERT INTO app_products(org,brand,name,content,sort_order,active,created_at,updated_at) "
                        "VALUES('johnson',?,?,?,?,1,?,?)", (brand.strip(), name.strip(), content, mx + 1, now, now))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def update_product(pid, **fields):
    _ensure_seeded()
    allowed = {'name', 'content', 'brand', 'active'}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return False
    sets.append("updated_at=?")
    vals.append(int(time.time()))
    vals.append(pid)
    c = _conn()
    try:
        c.execute(f"UPDATE app_products SET {', '.join(sets)} WHERE id=?", vals)
        c.commit()
        return True
    finally:
        c.close()


def deactivate_product(pid):
    return update_product(pid, active=0)


def update_guidelines(brand_name, guidelines):
    _ensure_seeded()
    c = _conn()
    try:
        c.execute("UPDATE app_brands SET guidelines=?, updated_at=? WHERE name=?",
                  (guidelines, int(time.time()), brand_name))
        c.commit()
        return True
    finally:
        c.close()


def reimport_from_json():
    """从 products.json 强制重建(清空 app_brands/app_products 再播种)。
    给后台「从文件重新导入」用:跑完 build_products.py 重建 JSON 后,一键刷进库。
    注意:会覆盖库里所有手工改动。"""
    c = _conn()
    try:
        _ensure_schema(c)
        c.execute("DELETE FROM app_products")
        c.execute("DELETE FROM app_brands")
        now = int(time.time())
        nb = npd = 0
        for bi, b in enumerate(_load_json().get('brands', [])):
            c.execute("INSERT INTO app_brands(org,name,guidelines,sort_order,active,created_at,updated_at) "
                      "VALUES('johnson',?,?,?,1,?,?)", (b.get('name', ''), b.get('guidelines', ''), bi, now, now))
            nb += 1
            for pi, p in enumerate(b.get('products', [])):
                c.execute("INSERT INTO app_products(org,brand,name,content,sort_order,active,created_at,updated_at) "
                          "VALUES('johnson',?,?,?,?,1,?,?)", (b.get('name', ''), p.get('name', ''), p.get('content', ''), pi, now, now))
                npd += 1
        c.commit()
        return {"brands": nb, "products": npd}
    finally:
        c.close()
