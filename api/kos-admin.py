"""POST /api/kos-admin —— KOS 素材库 + 任务管理(仅管理员)。

action:
  create_library {brand,product,code,note}     建库,返回应上传到的 COS 目录
  list_libraries                                列库(含容量)
  scan_library  {library_id}                    扫描该库 COS 目录,登记主图/可拼图
  list_materials {library_id}
  delete_material {id}
  create_task   {title,brand,product,library_id,scope,depts,per_person,deadline}
  list_tasks
  close_task    {id}

素材命名约定:文件名含「主图」→ 主图;含「可拼图」→ 可拼图;其余跳过。
COS 目录约定:kos/<品牌>/<产品>/<批次code>/  (建库时返回,管理员照此上传)
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import os
import sys
import json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.auth import is_admin
from lib import kos_store, users_store

KOS_PREFIX = os.environ.get('COS_KOS_PREFIX', 'kos/')
IMG_EXT = ('.jpg', '.jpeg', '.png', '.webp')


def _cos_client():
    from qcloud_cos import CosConfig, CosS3Client
    sid = os.environ.get('COS_SECRET_ID', '').strip()
    skey = os.environ.get('COS_SECRET_KEY', '').strip()
    region = os.environ.get('COS_REGION', '').strip()
    bucket = os.environ.get('COS_BUCKET', '').strip()
    if not all([sid, skey, region, bucket]):
        raise RuntimeError('COS 未配置,请在 .env 补齐 COS_SECRET_ID/KEY/REGION/BUCKET')
    client = CosS3Client(CosConfig(Region=region, SecretId=sid, SecretKey=skey, Scheme='https'))
    return client, bucket


def _list_cos(prefix):
    client, bucket = _cos_client()
    keys = []
    marker = ''
    while True:
        r = client.list_objects(Bucket=bucket, Prefix=prefix, Marker=marker, MaxKeys=1000)
        for obj in r.get('Contents', []):
            keys.append(obj['Key'])
        if r.get('IsTruncated') == 'true':
            marker = r.get('NextMarker', '')
        else:
            break
    return keys


def _classify(filename):
    if '主图' in filename:
        return kos_store.ROLE_MAIN
    if '可拼图' in filename:
        return kos_store.ROLE_TILE
    return None


def _users_in_scope(scope, depts):
    allu = users_store.all_users()
    if scope == 'dept' and depts:
        allu = [u for u in allu if u.get('department') in depts]
    return len(allu)


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length).decode("utf-8") if length else "{}")
            caller = req.get("_user") or {}
            if not is_admin(caller):
                return self._json(403, {"error": "无权访问,仅管理员可操作"})
            action = (req.get("action") or "").strip()

            if action == "create_library":
                brand = (req.get("brand") or "").strip()
                product = (req.get("product") or "").strip()
                code = (req.get("code") or "").strip()
                if not (brand and product):
                    return self._json(400, {"error": "请填写品牌和产品"})
                prefix = f"{KOS_PREFIX}{brand}/{product}/{code}/" if code else f"{KOS_PREFIX}{brand}/{product}/"
                lib_id = kos_store.create_library(brand, product, code, req.get("note") or "", prefix)
                return self._json(200, {"ok": True, "id": lib_id, "cos_prefix": prefix})

            if action == "list_libraries":
                return self._json(200, {"libraries": kos_store.list_libraries()})

            if action == "scan_library":
                lib = kos_store.get_library(req.get("library_id"))
                if not lib:
                    return self._json(404, {"error": "素材库不存在"})
                try:
                    keys = _list_cos(lib['cos_prefix'])
                except RuntimeError as e:
                    return self._json(503, {"error": str(e)})
                exist = kos_store.existing_cos_keys(lib['id'])
                added = {kos_store.ROLE_MAIN: 0, kos_store.ROLE_TILE: 0}
                skipped = 0
                idx = {kos_store.ROLE_MAIN: 0, kos_store.ROLE_TILE: 0}
                for key in sorted(keys):
                    fn = key.rsplit('/', 1)[-1]
                    if not fn.lower().endswith(IMG_EXT):
                        continue
                    if key in exist:
                        continue
                    role = _classify(fn)
                    if not role:
                        skipped += 1
                        continue
                    kos_store.add_material(lib['id'], role, key, fn, idx[role])
                    idx[role] += 1
                    added[role] += 1
                return self._json(200, {
                    "ok": True,
                    "added_mains": added[kos_store.ROLE_MAIN],
                    "added_tiles": added[kos_store.ROLE_TILE],
                    "skipped": skipped,
                    "capacity": kos_store.capacity(lib['id']),
                })

            if action == "list_materials":
                return self._json(200, {"materials": kos_store.list_materials(req.get("library_id"))})

            if action == "delete_material":
                mid = req.get("id")
                if not mid:
                    return self._json(400, {"error": "缺少素材 id"})
                # 软删除:active=0
                import sqlite3
                c = kos_store._conn()
                try:
                    c.execute("UPDATE kos_materials SET active=0 WHERE id=?", (mid,))
                    c.commit()
                finally:
                    c.close()
                return self._json(200, {"ok": True})

            if action == "create_task":
                library_id = req.get("library_id")
                if not library_id:
                    return self._json(400, {"error": "请选择素材库"})
                scope = (req.get("scope") or "all").strip()
                depts = req.get("depts") or []
                per_person = max(1, int(req.get("per_person") or 1))
                lib = kos_store.get_library(library_id)
                if not lib:
                    return self._json(404, {"error": "素材库不存在"})
                # 容量预警(不硬拦)
                need = _users_in_scope(scope, depts) * per_person
                remaining = kos_store.capacity(library_id)['remaining']
                tid = kos_store.create_task(
                    req.get("title") or f"{lib['brand']} {lib['product']} 种草任务",
                    lib['brand'], lib['product'], library_id, scope, depts, per_person,
                    req.get("deadline") or "", caller.get("name") or "")
                return self._json(200, {"ok": True, "id": tid, "need": need,
                                        "remaining": remaining, "enough": remaining >= need})

            if action == "list_tasks":
                return self._json(200, {"tasks": kos_store.list_tasks()})

            if action == "close_task":
                if not req.get("id"):
                    return self._json(400, {"error": "缺少任务 id"})
                kos_store.close_task(req.get("id"))
                return self._json(200, {"ok": True})

            return self._json(400, {"error": "未知 action"})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
