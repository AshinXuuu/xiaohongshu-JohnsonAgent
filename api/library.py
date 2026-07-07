"""产品资料库 —— 在线下载产品资料(单页 / 中英文说明书)。

资料文件存放在腾讯云对象存储(COS)私有桶里,本接口:
  - GET  /api/library                 → 返回资料清单(品牌→产品→文件,不含直链;
                                         每个文件带 thumb_url 供列表页显示封面缩略图)
  - GET  /api/library?thumb=1&key=<cos_key>&t=<token>
                                      → 令牌校验后返回该文件的封面缩略图(JPEG,本地缓存;
                                         <img> 带不了 Authorization 头,故用 HMAC 令牌放 URL,
                                         同 api/kos.py 的成品图下载)
  - POST /api/library  {key, _user}   → 校验 key 合法后,返回一条有时效的 COS 预签名下载链接,
                                         并记录一次 download 事件

设计要点:
  - 桶保持私有,链接由后端用密钥临时签发(默认 5 分钟过期),外泄也会失效
  - 只有出现在 manifest 里的 key 才允许下载(防止有人构造 key 去拿「价格与政策」等未开放资料)
  - COS 凭据全部读环境变量,不写死在代码里

需要在服务器 .env 配置:
  COS_SECRET_ID=...
  COS_SECRET_KEY=...
  COS_REGION=ap-shanghai          # 桶所在地域
  COS_BUCKET=xxxx-1250000000      # 桶名(含 APPID)
  COS_PREFIX=产品库/               # 上传时的 Key 前缀(若直接传了产品库文件夹就是「产品库/」)
  COS_URL_EXPIRE=300              # 链接有效期(秒),可选,默认 300
"""
import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
MANIFEST_PATH = ROOT / "data" / "library_manifest.json"
THUMBS_DIR = ROOT / "data" / "library_thumbs"
THUMB_WIDTH = 640
_THUMB_IMG_EXT = ('.jpg', '.jpeg', '.png', '.webp')


# ──────────────────────── 数据 ────────────────────────

def load_manifest() -> dict:
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"brands": {}}


def allowed_keys(manifest: dict) -> dict:
    """返回 {key: {brand, product, name, type}},既做白名单也做下载时的元信息。"""
    out = {}
    for brand, products in (manifest.get("brands") or {}).items():
        for p in products:
            for f in p.get("files", []):
                out[f["key"]] = {
                    "brand": brand,
                    "product": p["name"],
                    "name": f["name"],
                    "type": f.get("type", ""),
                }
    return out


# ──────────────────────── COS ────────────────────────

def cos_config():
    cfg = {
        "secret_id": os.environ.get("COS_SECRET_ID", "").strip(),
        "secret_key": os.environ.get("COS_SECRET_KEY", "").strip(),
        "region": os.environ.get("COS_REGION", "").strip(),
        "bucket": os.environ.get("COS_BUCKET", "").strip(),
        "prefix": os.environ.get("COS_PREFIX", "产品库/"),
        "expire": int(os.environ.get("COS_URL_EXPIRE", "300") or "300"),
    }
    return cfg


def make_presigned_url(key: str, filename: str) -> str:
    """用 COS SDK 生成一条预签名下载链接;强制浏览器下载并保留中文文件名。"""
    from qcloud_cos import CosConfig, CosS3Client

    cfg = cos_config()
    missing = [k for k in ("secret_id", "secret_key", "region", "bucket") if not cfg[k]]
    if missing:
        raise RuntimeError(f"COS 未配置(缺 {', '.join(missing)}),请在服务器 .env 补齐")

    config = CosConfig(
        Region=cfg["region"],
        SecretId=cfg["secret_id"],
        SecretKey=cfg["secret_key"],
        Scheme="https",
    )
    client = CosS3Client(config)
    object_key = f"{cfg['prefix']}{key}"

    # 让浏览器以「下载」方式保存,并用 RFC5987 编码保住中文文件名
    disp = "attachment; filename*=UTF-8''" + quote(filename)
    return client.get_presigned_download_url(
        Bucket=cfg["bucket"],
        Key=object_key,
        Expired=cfg["expire"],
        Params={"response-content-disposition": disp},
    )


# ──────────────────────── 缩略图 ────────────────────────

def _thumb_secret() -> bytes:
    """缩略图令牌的签名密钥。同 api/kos.py 的 _img_secret:
    优先读专用密钥 KOS_IMG_SECRET,回退到已有的高熵 SESSION_SECRET;
    都没有则直接报错,绝不使用可预测的常量。"""
    s = (os.environ.get('KOS_IMG_SECRET')
         or os.environ.get('SESSION_SECRET')
         or os.environ.get('DEEPSEEK_API_KEY'))
    if not s:
        raise RuntimeError('缩略图令牌密钥未配置,请在 .env 设置 KOS_IMG_SECRET')
    return s.encode('utf-8')


def thumb_token(key: str) -> str:
    """key → HMAC-SHA256 令牌(消息前缀 'libthumb:',与 KOS 成品图令牌区分)。"""
    msg = ('libthumb:' + key).encode('utf-8')
    return hmac.new(_thumb_secret(), msg, hashlib.sha256).hexdigest()


def thumb_url(key: str) -> str:
    return f"/api/library?thumb=1&key={quote(key)}&t={thumb_token(key)}"


def thumb_cache_path(key: str) -> Path:
    return THUMBS_DIR / (hashlib.sha256(key.encode('utf-8')).hexdigest()[:16] + '.jpg')


def pdf_to_thumb(src: str, dest: str, width: int = THUMB_WIDTH) -> bool:
    """PDF 第 1 页 → JPG 缩略图(宽约 width px,质量 72)。
    PyMuPDF 可能未安装,故 import fitz 必须放在函数内 —— 失败只让缩略图不可用(前端降级),
    绝不能拖垮整个模块;渲染失败(损坏 PDF 等)同样返回 False。"""
    try:
        import fitz  # PyMuPDF
    except Exception as e:
        print(f"[library] PyMuPDF 不可用,PDF 缩略图跳过:{e!r}", flush=True)
        return False
    try:
        from PIL import Image
        doc = fitz.open(src)
        try:
            page = doc.load_page(0)
            zoom = width / max(page.rect.width, 1.0)
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        finally:
            doc.close()
        img.save(dest, "JPEG", quality=72)
        return True
    except Exception as e:
        print(f"[library] PDF 缩略图生成失败({src}):{e!r}", flush=True)
        return False


def image_to_thumb(src: str, dest: str, width: int = THUMB_WIDTH) -> bool:
    """图片(jpg/png/webp)→ 宽 width 的 JPG 缩略图。失败返回 False。"""
    try:
        from PIL import Image
        img = Image.open(src).convert("RGB")
        if img.width > width:
            img = img.resize((width, max(1, round(img.height * width / img.width))), Image.LANCZOS)
        img.save(dest, "JPEG", quality=72)
        return True
    except Exception as e:
        print(f"[library] 图片缩略图生成失败({src}):{e!r}", flush=True)
        return False


def build_thumb(key: str, cache: Path) -> bool:
    """从 COS 拉源文件生成缩略图写入 cache。不支持的类型 / 任一步失败都返回 False(上层回 404)。"""
    ext = os.path.splitext(key)[1].lower()
    if ext != '.pdf' and ext not in _THUMB_IMG_EXT:
        return False
    tmp = tempfile.mkdtemp(prefix="libthumb_")
    try:
        src = os.path.join(tmp, "src" + ext)
        try:
            from qcloud_cos import CosConfig, CosS3Client
            cfg = cos_config()
            missing = [k for k in ("secret_id", "secret_key", "region", "bucket") if not cfg[k]]
            if missing:
                print(f"[library] COS 未配置(缺 {', '.join(missing)}),缩略图不可用", flush=True)
                return False
            client = CosS3Client(CosConfig(
                Region=cfg["region"], SecretId=cfg["secret_id"],
                SecretKey=cfg["secret_key"], Scheme="https"))
            resp = client.get_object(Bucket=cfg["bucket"], Key=f"{cfg['prefix']}{key}")
            resp["Body"].get_stream_to_file(src)
        except Exception as e:
            print(f"[library] COS 拉取源文件失败({key}):{e!r}", flush=True)
            return False
        out = os.path.join(tmp, "thumb.jpg")
        ok = pdf_to_thumb(src, out) if ext == '.pdf' else image_to_thumb(src, out)
        if not ok:
            return False
        cache.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(out, str(cache))   # 先在临时目录出图再移入缓存,避免半成品被命中
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def attach_thumb_urls(brands: dict):
    """给清单里每个文件补 thumb_url(带 HMAC 令牌,供 <img> 直接引用)。
    密钥未配置等异常时静默跳过 —— 前端按无缩略图降级,清单本身不受影响。"""
    try:
        for products in (brands or {}).values():
            for p in products:
                for f in p.get("files", []):
                    key = f.get("key") or ""
                    if key:
                        f["thumb_url"] = thumb_url(key)
    except Exception as e:
        print(f"[library] thumb_url 生成跳过:{e!r}", flush=True)


# ──────────────────────── 日志 ────────────────────────

def log_download(user: dict, info: dict):
    try:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from lib.kv_store import log_event
        log_event("download", user or {}, {
            "brand": info.get("brand"),
            "product": info.get("product"),
            "file": info.get("name"),
            "file_type": info.get("type"),
        })
    except Exception:
        pass


# ──────────────────────── HTTP handler ────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        """GET /api/library[?action=manifest] → 资料清单(不含直链)
        GET /api/library?thumb=1&key=...&t=... → 封面缩略图(JPEG,令牌校验)"""
        try:
            qs = parse_qs(urlparse(self.path).query)
            if qs.get("thumb"):
                return self._thumb(qs)
            action = (qs.get("action", ["manifest"])[0]).strip()
            if action in ("", "manifest"):
                from lib.library_store import grouped
                brands = grouped()
                attach_thumb_urls(brands)
                return self._json(200, {"brands": brands})
            return self._error(400, "未知 action")
        except Exception as e:
            import traceback; traceback.print_exc()
            print("[API-500] " + getattr(self, "path", "") + " " + repr(e), flush=True)
            self._error(500, "服务器开小差了,请稍后重试")

    def _thumb(self, qs):
        """令牌校验 + 白名单校验后返回缩略图;命中 data/library_thumbs/ 缓存则直接回。"""
        key = (qs.get("key", [""])[0]).strip()
        tok = (qs.get("t", [""])[0]).strip()
        if not key or not tok:
            return self._error(400, "缺少参数")
        try:
            expect = thumb_token(key)
        except RuntimeError as e:
            print(f"[library] 缩略图密钥缺失:{e}", flush=True)
            return self._error(404, "缩略图不可用")
        if not hmac.compare_digest(tok, expect):
            return self._error(403, "无权访问")
        from lib.library_store import allowed_keys as _allowed
        if key not in _allowed():
            # 不在白名单 → 拒绝(与下载同一道闸,防止越权渲染未开放资料)
            return self._error(403, "该资料不在可访问范围内")
        cache = thumb_cache_path(key)
        if not cache.exists() and not build_thumb(key, cache):
            return self._error(404, "缩略图不可用")
        data = cache.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        """POST /api/library {key, _user} → 返回预签名下载链接"""
        req = {}
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            req = json.loads(body)

            key = (req.get("key") or "").strip()
            from lib.session import user_from_headers
            user = user_from_headers(self.headers)
            if not user:
                return self._error(401, "未登录或登录已过期,请重新登录")
            if not key:
                return self._error(400, "缺少 key")

            from lib.library_store import allowed_keys as _allowed
            info = _allowed().get(key)
            if not info:
                # 不在白名单 → 拒绝(防止越权拿未开放资料)
                return self._error(403, "该资料不在可下载范围内")

            try:
                url = make_presigned_url(key, info["name"])
            except RuntimeError as e:
                # 配置缺失:给前端一个明确提示
                return self._error(503, str(e))

            log_download(user, info)
            self._json(200, {"url": url, "name": info["name"]})
        except Exception as e:
            import traceback; traceback.print_exc()
            print("[API-500] " + getattr(self, "path", "") + " " + repr(e), flush=True)
            self._error(500, "服务器开小差了,请稍后重试")

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, msg):
        self._json(code, {"error": msg})
