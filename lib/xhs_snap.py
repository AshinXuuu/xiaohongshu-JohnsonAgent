# -*- coding: utf-8 -*-
"""小红书笔记快照:业务提交链接时,服务端抓一次页面内嵌 JSON,
存下 笔记ID / 标题 / 简介 / 点赞数 / 封面缩略图,给后台看板做存证与展示。

原理(2026-07 实测):xhslink 短链 302 → xiaohongshu.com 笔记页,移动端 UA 访问
返回的 HTML 内嵌 window.__INITIAL_STATE__,含 noteData(title/desc/imageList/interactInfo),
无需登录。抓取失败(风控/删帖/超时)不影响业务提交,后台显示「无快照」。

设计约束:
    - 抓取在后台线程执行,绝不阻塞业务的提交请求
    - 每次提交只抓一次;同一链接已有快照则跳过
    - 全站抓取节流(默认最小间隔 3 秒),避免高频触发风控
"""
import json
import re
import ssl
import time
import threading
import urllib.request
from pathlib import Path

from lib.db import get_conn as connect

ROOT = Path(__file__).resolve().parent.parent
SNAP_DIR = ROOT / 'data' / 'kos_snaps'

_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
       'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1')
_NOTE_ID_RE = re.compile(r'/(?:item|explore|discovery/item)/([0-9a-f]{24})', re.I)

_lock = threading.Lock()
_last_fetch = [0.0]
_MIN_INTERVAL = 3.0          # 全站抓取最小间隔(秒)


import hmac as _hmac
import hashlib as _hashlib
import os as _os


def snap_token(note_id):
    """快照封面图访问令牌(与 KOS 成品图同密钥体系)。"""
    sec = (_os.environ.get('KOS_IMG_SECRET') or _os.environ.get('SESSION_SECRET')
           or _os.environ.get('DEEPSEEK_API_KEY') or '')
    if not sec:
        raise RuntimeError('KOS_IMG_SECRET/SESSION_SECRET 未配置')
    return _hmac.new(sec.encode(), f'snap:{note_id}'.encode(), _hashlib.sha256).hexdigest()


def _ensure():
    c = connect()
    try:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS kos_link_snaps (
                url_key   TEXT PRIMARY KEY,      -- 归一化后的提交链接
                note_id   TEXT,
                final_url TEXT,
                title     TEXT,
                note_desc TEXT,
                liked     TEXT,
                has_cover INTEGER DEFAULT 0,
                status    TEXT,                  -- ok / fail
                fetched_at INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_ksnap_note ON kos_link_snaps(note_id);
        """)
        c.commit()
    finally:
        c.close()


def norm_url(u):
    return str(u or '').strip().rstrip('/')


def get_snap(url):
    _ensure()
    c = connect()
    try:
        r = c.execute("SELECT * FROM kos_link_snaps WHERE url_key=?", (norm_url(url),)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


def snaps_for_urls(urls):
    """批量取快照,返回 {url_key: row_dict}。"""
    _ensure()
    keys = list({norm_url(u) for u in urls if u})
    if not keys:
        return {}
    c = connect()
    try:
        out = {}
        for i in range(0, len(keys), 500):
            batch = keys[i:i + 500]
            q = ("SELECT * FROM kos_link_snaps WHERE url_key IN (%s)"
                 % ','.join('?' * len(batch)))
            for r in c.execute(q, batch).fetchall():
                out[r['url_key']] = dict(r)
        return out
    finally:
        c.close()


def _save(url_key, **kw):
    _ensure()
    c = connect()
    try:
        c.execute(
            "INSERT INTO kos_link_snaps(url_key,note_id,final_url,title,note_desc,liked,has_cover,status,fetched_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(url_key) DO UPDATE SET note_id=excluded.note_id, final_url=excluded.final_url, "
            "title=excluded.title, note_desc=excluded.note_desc, liked=excluded.liked, has_cover=excluded.has_cover, "
            "status=excluded.status, fetched_at=excluded.fetched_at",
            (url_key, kw.get('note_id'), kw.get('final_url'), kw.get('title'),
             kw.get('note_desc'), kw.get('liked'), int(kw.get('has_cover') or 0),
             kw.get('status', 'fail'), int(time.time())))
        c.commit()
    finally:
        c.close()


def _http_get(url, timeout=12, max_bytes=2_000_000, binary=False):
    req = urllib.request.Request(url, headers={'User-Agent': _UA})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        data = r.read(max_bytes)
        return r.geturl(), (data if binary else data.decode('utf-8', 'ignore'))


def _find_note_dict(obj):
    """在 __INITIAL_STATE__ 里递归找同时含 title 和 imageList 的字典(即 noteData)。"""
    if isinstance(obj, dict):
        if 'title' in obj and 'imageList' in obj:
            return obj
        for v in obj.values():
            r = _find_note_dict(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_note_dict(v)
            if r:
                return r
    return None


def _first_image_url(note):
    """imageList 第一张里找出第一个 http 图片地址。"""
    imgs = note.get('imageList') or []
    def walk(o):
        if isinstance(o, str) and o.startswith('http'):
            return o
        if isinstance(o, dict):
            for v in o.values():
                r = walk(v)
                if r:
                    return r
        if isinstance(o, list):
            for v in o:
                r = walk(v)
                if r:
                    return r
        return None
    return walk(imgs[0]) if imgs else None


def _make_thumb(raw, dest, width=320):
    from io import BytesIO
    from PIL import Image
    with Image.open(BytesIO(raw)) as img:
        img = img.convert('RGB')
        if img.width > width:
            img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        img.save(dest, 'JPEG', quality=82)


def capture(url):
    """抓取并落库;任何异常只记 fail 不上抛。适合放后台线程调用。"""
    key = norm_url(url)
    if not key:
        return
    try:
        old = get_snap(key)
        if old and old.get('status') == 'ok':
            return                      # 已有成功快照,不重抓
        with _lock:                     # 节流:全站串行 + 最小间隔
            wait = _MIN_INTERVAL - (time.time() - _last_fetch[0])
            if wait > 0:
                time.sleep(wait)
            _last_fetch[0] = time.time()
        final_url, html = _http_get(key)
        m = _NOTE_ID_RE.search(final_url) or _NOTE_ID_RE.search(key)
        note_id = m.group(1) if m else None
        info = {'note_id': note_id, 'final_url': final_url.split('?')[0] if final_url else None}
        mm = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?)\s*</script>', html, re.S)
        if mm:
            txt = re.sub(r'\bundefined\b', 'null', mm.group(1))
            try:
                note = _find_note_dict(json.loads(txt))
            except Exception:
                note = None
            if note:
                info['title'] = (note.get('title') or '')[:200]
                info['note_desc'] = (note.get('desc') or '')[:300]
                liked = ((note.get('interactInfo') or {}).get('likedCount') or '')
                info['liked'] = str(liked)[:20]
                cover = _first_image_url(note)
                if cover and note_id:
                    try:
                        _, raw = _http_get(cover, timeout=12, max_bytes=8_000_000, binary=True)
                        _make_thumb(raw, SNAP_DIR / f'{note_id}.jpg')
                        info['has_cover'] = 1
                    except Exception as e:
                        print(f'[SNAP] 封面下载失败 {note_id}: {e}', flush=True)
        if info.get('title'):
            info['status'] = 'ok'
            _save(key, **info)
            print(f'[SNAP] ok {key} -> {info.get("note_id")} {info.get("title")[:30]}', flush=True)
        else:
            info['status'] = 'fail'
            _save(key, **info)
            print(f'[SNAP] 无笔记数据(可能风控/删帖) {key}', flush=True)
    except Exception as e:
        try:
            _save(key, status='fail')
        except Exception:
            pass
        print(f'[SNAP] 抓取失败 {key}: {e}', flush=True)


def capture_async(url):
    threading.Thread(target=capture, args=(url,), daemon=True).start()


def backfill_missing(urls, limit=5):
    """给历史链接补快照:挑出没抓过的,后台最多补 limit 条(逐步回填)。"""
    _ensure()
    have = snaps_for_urls(urls)
    todo = [u for u in {norm_url(x) for x in urls if x} if u not in have][:limit]
    for u in todo:
        capture_async(u)
    return len(todo)
