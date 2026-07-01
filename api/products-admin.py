"""POST /api/products-admin —— 后台产品管理(仅管理员)。

action:
  list              列出品牌(含 guidelines)+ 产品(含 id/content)
  add_product       新增产品 {brand, name, content}
  update_product    改产品 {id, name?, content?}
  delete_product    停用产品 {id}
  update_guidelines 改品牌资料 {brand, guidelines}
  reimport          从 products.json 强制重建(批量刷新,覆盖手工改动)

权限:一律服务端用 lib.auth 核对管理员,不信任前端。
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import os
import sys
import json

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.auth import is_admin
from lib import products_store, library_store

# 资料库文件在 COS 的前缀(与 api/library.py 下载一致)
LIB_PREFIX = os.environ.get('COS_PREFIX', 'kb/原始素材/产品库/')
FILE_TYPES = ('单页', '中文说明书', '英文说明书', '其他')


def _cos_client():
    from qcloud_cos import CosConfig, CosS3Client
    sid = os.environ.get('COS_SECRET_ID', '').strip()
    skey = os.environ.get('COS_SECRET_KEY', '').strip()
    region = os.environ.get('COS_REGION', '').strip()
    bucket = os.environ.get('COS_BUCKET', '').strip()
    if not all([sid, skey, region, bucket]):
        raise RuntimeError('COS 未配置')
    return CosS3Client(CosConfig(Region=region, SecretId=sid, SecretKey=skey, Scheme='https')), bucket


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
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            req = json.loads(body)

            if not is_admin(req.get("_user") or {}):
                return self._json(403, {"error": "无权访问,仅管理员可操作"})
            action = (req.get("action") or "").strip()

            if action == "list":
                return self._json(200, {"brands": products_store.get_all()})

            if action == "add_product":
                brand = (req.get("brand") or "").strip()
                name = (req.get("name") or "").strip()
                content = req.get("content") or ""
                if not (brand and name):
                    return self._json(400, {"error": "请填写品牌和产品名"})
                pid = products_store.add_product(brand, name, content)
                return self._json(200, {"ok": True, "id": pid})

            if action == "update_product":
                pid = req.get("id")
                if not pid:
                    return self._json(400, {"error": "缺少产品 id"})
                fields = {}
                if "name" in req:
                    fields["name"] = (req.get("name") or "").strip()
                if "content" in req:
                    fields["content"] = req.get("content") or ""
                if not fields:
                    return self._json(400, {"error": "没有要更新的字段"})
                products_store.update_product(pid, **fields)
                return self._json(200, {"ok": True})

            if action == "delete_product":
                pid = req.get("id")
                if not pid:
                    return self._json(400, {"error": "缺少产品 id"})
                products_store.deactivate_product(pid)
                return self._json(200, {"ok": True})

            if action == "update_guidelines":
                brand = (req.get("brand") or "").strip()
                if not brand:
                    return self._json(400, {"error": "缺少品牌"})
                products_store.update_guidelines(brand, req.get("guidelines") or "")
                return self._json(200, {"ok": True})

            if action == "reimport":
                res = products_store.reimport_from_json()
                return self._json(200, {"ok": True, **res})

            # ── 产品资料库文件管理 ──
            if action == "list_files":
                return self._json(200, {"files": library_store.list_by_product(
                    (req.get("brand") or "").strip(), (req.get("product") or "").strip())})

            if action == "sign_file_upload":
                brand = (req.get("brand") or "").strip()
                product = (req.get("product") or "").strip()
                files = req.get("files") or []
                if not (brand and product and files):
                    return self._json(400, {"error": "缺少品牌/产品/文件"})
                try:
                    client, bucket = _cos_client()
                except RuntimeError as e:
                    return self._json(503, {"error": str(e)})
                import uuid
                out = []
                for fn in files:
                    ext = os.path.splitext(str(fn))[1].lower() or '.pdf'
                    rel_key = f"{brand}/{product}/{uuid.uuid4().hex[:10]}{ext}"   # 存库的相对 key
                    full_key = f"{LIB_PREFIX}{rel_key}"                            # COS 实际对象 key
                    url = client.get_presigned_url(Method='PUT', Bucket=bucket, Key=full_key, Expired=900)
                    out.append({"filename": fn, "cos_key": rel_key, "put_url": url})
                return self._json(200, {"ok": True, "uploads": out})

            if action == "register_files":
                brand = (req.get("brand") or "").strip()
                product = (req.get("product") or "").strip()
                n = 0
                for it in (req.get("items") or []):
                    key = (it.get("cos_key") or "").strip()
                    if not key or library_store.key_exists(key):
                        continue
                    ftype = it.get("type") if it.get("type") in FILE_TYPES else "单页"
                    library_store.add_file(brand, product, it.get("filename") or key.rsplit('/', 1)[-1],
                                           ftype, key, it.get("size") or 0)
                    n += 1
                return self._json(200, {"ok": True, "added": n})

            if action == "update_file":
                fid = req.get("id")
                if not fid:
                    return self._json(400, {"error": "缺少文件 id"})
                name = req.get("name")
                ftype = req.get("type") if req.get("type") in FILE_TYPES else None
                library_store.update_file(fid, name=(name.strip() if name else None), ftype=ftype)
                return self._json(200, {"ok": True})

            if action == "delete_file":
                if not req.get("id"):
                    return self._json(400, {"error": "缺少文件 id"})
                library_store.deactivate_file(req.get("id"))
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
