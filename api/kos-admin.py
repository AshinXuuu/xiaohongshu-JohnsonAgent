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


def _classify(key):
    """按 COS key 判定角色:优先看 主图/ 或 可拼图/ 子目录(网页上传),再看文件名关键词。"""
    if '/主图/' in key or key.startswith('主图/'):
        return kos_store.ROLE_MAIN
    if '/可拼图/' in key or key.startswith('可拼图/'):
        return kos_store.ROLE_TILE
    fn = key.rsplit('/', 1)[-1]
    if '主图' in fn:
        return kos_store.ROLE_MAIN
    if '可拼图' in fn:
        return kos_store.ROLE_TILE
    return None


def _sync_library(lib):
    """把该库的 COS 目录与数据库素材对账:COS 有的登记进来,COS 删掉的下架。
    返回 True 表示成功读到了 COS(数量已与桶对齐);False 表示 COS 不可达(退回数据库现有记录)。"""
    try:
        client, bucket = _cos_client()
    except Exception:
        return False
    prefix = lib.get('cos_prefix') or ''
    if not prefix:
        return False
    keys = []
    marker = ''
    try:
        while True:
            r = client.list_objects(Bucket=bucket, Prefix=prefix, Marker=marker, MaxKeys=1000)
            for o in r.get('Contents', []):
                if o['Key'].lower().endswith(IMG_EXT):
                    keys.append(o['Key'])
            if r.get('IsTruncated') == 'true':
                marker = r.get('NextMarker', '')
            else:
                break
    except Exception:
        return False
    cos_set = set(keys)
    existing = {m['cos_key']: m for m in kos_store.list_materials(lib['id'])}
    idx = {kos_store.ROLE_MAIN: 0, kos_store.ROLE_TILE: 0}
    # COS 有、库没有 → 登记
    for k in sorted(cos_set - set(existing)):
        role = _classify(k)
        if role:
            kos_store.add_material(lib['id'], role, k, k.rsplit('/', 1)[-1], idx[role])
            idx[role] += 1
    # 库有、COS 已删 → 下架
    for k, m in existing.items():
        if k not in cos_set:
            kos_store.deactivate_material(m['id'])


def _users_in_scope(scope, depts):
    allu = users_store.all_users()
    if scope == 'dept' and depts:
        allu = [u for u in allu if u.get('department') in depts]
    return len(allu)


def _audit_log(caller, action, req):
    from lib.audit import log
    s = None
    if action == 'create_library':
        s = f"新建素材库 {req.get('brand')} / {req.get('product')}"
    elif action == 'delete_library':
        s = f"删除素材库(id {req.get('library_id')})"
    elif action == 'register_materials':
        s = f"上传KOS素材 {req.get('role')}({len(req.get('items') or [])} 张)"
    elif action == 'scan_library':
        s = f"扫描登记素材库(id {req.get('library_id')})"
    elif action == 'delete_material':
        s = f"删除KOS素材(id {req.get('id')})"
    elif action == 'create_task':
        s = f"发布任务(库id {req.get('library_id')},{req.get('scope')},每人{req.get('per_person') or 1}篇)"
    elif action == 'close_task':
        s = f"结束任务(id {req.get('id')})"
    if s:
        log(caller, '任务', action, s)


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
            _audit_log(caller, action, req)

            if action == "create_library":
                brand = (req.get("brand") or "").strip()
                product = (req.get("product") or "").strip()
                code = (req.get("code") or "").strip()
                if not (brand and product):
                    return self._json(400, {"error": "请选择品牌和产品"})
                if not code:
                    import datetime
                    code = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')  # 日期时间批次,天然不重名
                prefix = f"{KOS_PREFIX}{brand}/{product}/{code}/"
                lib_id = kos_store.create_library(brand, product, code, req.get("note") or "", prefix)
                return self._json(200, {"ok": True, "id": lib_id, "cos_prefix": prefix})

            if action == "list_libraries":
                libs = kos_store.list_libraries()
                any_synced = False
                for lib in libs:                     # 打开页面即与 COS 对账,数量实时反映桶内实际
                    try:
                        ok = _sync_library(lib)
                    except Exception:
                        ok = False
                    any_synced = any_synced or ok
                    lib['capacity'] = kos_store.capacity(lib['id'])
                    lib['cos_synced'] = ok
                return self._json(200, {"libraries": libs, "cos_synced": any_synced or not libs})

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
                    role = _classify(key)
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
                if not req.get("id"):
                    return self._json(400, {"error": "缺少素材 id"})
                kos_store.deactivate_material(req.get("id"))
                return self._json(200, {"ok": True})

            if action == "delete_library":
                lib = kos_store.get_library(req.get("library_id"))
                if not lib:
                    return self._json(400, {"error": "素材库不存在"})
                # 一并删除该库 COS 目录下所有对象(有写权限)
                deleted = 0
                try:
                    client, bucket = _cos_client()
                    marker = ''
                    while True:
                        r = client.list_objects(Bucket=bucket, Prefix=lib['cos_prefix'], Marker=marker, MaxKeys=1000)
                        objs = r.get('Contents', [])
                        if objs:
                            client.delete_objects(Bucket=bucket, Delete={'Object': [{'Key': o['Key']} for o in objs]})
                            deleted += len(objs)
                        if r.get('IsTruncated') == 'true':
                            marker = r.get('NextMarker', '')
                        else:
                            break
                except Exception:
                    pass  # COS 删除失败不阻塞记录停用
                kos_store.deactivate_library(lib['id'])
                return self._json(200, {"ok": True, "cos_deleted": deleted})

            if action == "sign_uploads":
                # 为浏览器直传 COS 签发一批预签名 PUT 链接(需子账号有写权限)
                lib = kos_store.get_library(req.get("library_id"))
                if not lib:
                    return self._json(404, {"error": "素材库不存在"})
                role = (req.get("role") or "").strip()
                if role not in (kos_store.ROLE_MAIN, kos_store.ROLE_TILE):
                    return self._json(400, {"error": "角色不合法"})
                files = req.get("files") or []
                if not files:
                    return self._json(400, {"error": "没有文件"})
                try:
                    client, bucket = _cos_client()
                except RuntimeError as e:
                    return self._json(503, {"error": str(e)})
                import os as _os
                import uuid
                out = []
                for fn in files:
                    ext = _os.path.splitext(str(fn))[1].lower() or '.jpg'
                    key = f"{lib['cos_prefix']}{role}/{uuid.uuid4().hex[:10]}{ext}"
                    url = client.get_presigned_url(Method='PUT', Bucket=bucket, Key=key, Expired=900)
                    out.append({"filename": fn, "cos_key": key, "put_url": url})
                return self._json(200, {"ok": True, "uploads": out})

            if action == "register_materials":
                lib = kos_store.get_library(req.get("library_id"))
                if not lib:
                    return self._json(404, {"error": "素材库不存在"})
                role = (req.get("role") or "").strip()
                if role not in (kos_store.ROLE_MAIN, kos_store.ROLE_TILE):
                    return self._json(400, {"error": "角色不合法"})
                exist = kos_store.existing_cos_keys(lib['id'])
                n = 0
                for it in (req.get("items") or []):
                    key = (it.get("cos_key") or "").strip()
                    if key and key not in exist:
                        kos_store.add_material(lib['id'], role, key, it.get("filename", ""))
                        n += 1
                return self._json(200, {"ok": True, "added": n, "capacity": kos_store.capacity(lib['id'])})

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
