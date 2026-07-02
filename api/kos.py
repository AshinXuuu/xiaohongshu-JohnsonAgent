"""KOS 业务侧接口。

POST /api/kos
  action=my_tasks                     我的待办任务 + 进度
  action=issue   {task_id}            领一份素材包:挑唯一组合 → COS 拉源图 → 拼图 → 文案
                                        → 本地出图 → 返回 3 图(带令牌的下载地址)+ 文案
  action=complete {pack_id, note_url} 回填小红书链接 → 标记已发布
  action=leaderboard                  排行榜(完成任务数 + 笔记数)
  action=my_packs {task_id?}          我领过的记录

GET /api/kos?img=1&pack=<id>&kind=cover|two|four&t=<token>
  令牌校验后返回本地成品 JPEG(成品图不写 COS,规避只读密钥限制)。
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import os
import sys
import json
import hashlib
import random
import shutil
import tempfile
import mimetypes
import importlib.util

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lib import kos_store, products_store
from lib.auth import is_admin
from lib.image_compose import stack_vertical, grid_2x2, crop_cover

KOS_OUT = ROOT / 'data' / 'kos_out'
IMG_SECRET = os.environ.get('KOS_IMG_SECRET', 'kos-johnson-img-secret')
COPY_TYPE = '种草'
_IMG_EXT = ('.jpg', '.jpeg', '.png', '.webp')


def _ext(key):
    e = os.path.splitext(key)[1].lower()
    return e if e in _IMG_EXT else '.jpg'


def _token(pack_id, emp_id):
    return hashlib.sha256(f"{pack_id}:{emp_id}:{IMG_SECRET}".encode('utf-8')).hexdigest()[:16]


def _cos_client():
    from qcloud_cos import CosConfig, CosS3Client
    sid = os.environ.get('COS_SECRET_ID', '').strip()
    skey = os.environ.get('COS_SECRET_KEY', '').strip()
    region = os.environ.get('COS_REGION', '').strip()
    bucket = os.environ.get('COS_BUCKET', '').strip()
    if not all([sid, skey, region, bucket]):
        raise RuntimeError('COS 未配置')
    return CosS3Client(CosConfig(Region=region, SecretId=sid, SecretKey=skey, Scheme='https')), bucket


def _download(client, bucket, key, dest):
    resp = client.get_object(Bucket=bucket, Key=key)
    resp['Body'].get_stream_to_file(dest)
    return dest


def _gen_module():
    spec = importlib.util.spec_from_file_location('kos_genmod', str(ROOT / 'api' / 'generate.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# 种草角度池:每次领取随机取一个,保证同一产品每个人拿到的文案各不相同(整体仍是种草)
_COPY_ANGLES = [
    "从真实使用场景切入,突出日常怎么用、用起来的感受",
    "从使用前后的效果对比切入,描述看得见的变化",
    "从适合人群切入,说清楚谁最需要、为什么适合",
    "从新手小白视角切入,讲第一次上手的体验和顾虑打消",
    "从和同类产品对比切入,突出这款的差异化亮点",
    "从一个具体生活痛点切入,讲这款怎么解决",
    "从性价比与长期价值切入,算一笔值不值的账",
    "从坚持打卡/习惯养成切入,带出陪伴感和成就感",
]


def _make_copy(brand_name, product_name):
    """复用生成助手能力产出【种草】文案;每次随机换一个种草角度 → 每人不同。
    失败返回空结构,不阻塞出图。"""
    try:
        gen = _gen_module()
        brand, product = products_store.find_product(brand_name, product_name)
        if not product:
            return {"titles": [], "body": "", "tags": []}
        angle = random.choice(_COPY_ANGLES)
        sysp = gen.load_prompt(COPY_TYPE)
        userp = gen.build_user_message(brand, product, COPY_TYPE,
                                       f"本篇请{angle}。语气自然口语、真实分享,避免与常见模板雷同。")
        raw = gen.call_deepseek(sysp, userp)
        return gen.parse_model_output(raw)
    except Exception:
        return {"titles": [], "body": "", "tags": []}


def _img_url(pack_id, kind, emp_id):
    return f"/api/kos?img=1&pack={pack_id}&kind={kind}&t={_token(pack_id, emp_id)}"


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    # ───────── 成品图下载(令牌校验)─────────
    def do_GET(self):
        try:
            qs = parse_qs(urlparse(self.path).query)
            if qs.get('img'):
                pack_id = qs.get('pack', [''])[0]
                kind = qs.get('kind', [''])[0]
                tok = qs.get('t', [''])[0]
                if kind not in ('cover', 'two', 'four'):
                    return self._err(400, "bad kind")
                pack = kos_store.get_pack(pack_id)
                if not pack or tok != _token(pack['id'], pack['emp_id']):
                    return self._err(403, "无权访问")
                if kind == 'cover':
                    # 主图直出:保持原始格式(可能是 png/webp/jpg)
                    matches = sorted(KOS_OUT.glob(f"{pack['id']}_cover.*"))
                    if not matches:
                        return self._err(404, "图片不存在")
                    fpath = matches[0]
                    ctype = mimetypes.guess_type(str(fpath))[0] or "image/jpeg"
                else:
                    fpath = KOS_OUT / f"{pack['id']}_{kind}.jpg"
                    ctype = "image/jpeg"
                if not fpath.exists():
                    return self._err(404, "图片不存在")
                data = fpath.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=86400")
                self.end_headers()
                self.wfile.write(data)
                return
            return self._err(400, "缺少参数")
        except Exception as e:
            self._err(500, str(e))

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length).decode("utf-8") if length else "{}")
            from lib.session import user_from_headers
            user = user_from_headers(self.headers)
            if not user:
                return self._json(401, {"error": "未登录或登录已过期,请重新登录"})
            emp = (user.get("emp_id") or "").strip()
            if not emp:
                return self._json(401, {"error": "未登录"})
            action = (req.get("action") or "").strip()

            if action == "my_tasks":
                return self._json(200, {"tasks": kos_store.tasks_for_user(user)})

            if action == "leaderboard":
                return self._json(200, {"board": kos_store.leaderboard()})

            if action == "my_packs":
                return self._json(200, {"packs": kos_store.my_packs(emp, req.get("task_id"))})

            if action == "complete":
                res = kos_store.publish_pack(req.get("pack_id"), emp, req.get("note_url") or "")
                if res == 'bad_url':
                    return self._json(400, {"error": "链接无效,请粘贴正确的小红书笔记链接。"})
                if res == 'not_owner' or res is False:
                    return self._json(400, {"error": "回填失败(记录不存在或非本人)"})
                return self._json(200, {"ok": True})

            if action == "issue":
                return self._issue(req, user, emp)

            return self._json(400, {"error": "未知 action"})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _issue(self, req, user, emp):
        task = kos_store.get_task(req.get("task_id"))
        if not task or task.get("status") != "open":
            return self._json(404, {"error": "任务不存在或已结束"})
        if task["scope"] == "dept" and user.get("department") not in (task.get("depts") or []):
            return self._json(403, {"error": "该任务不在你的部门范围内"})
        mine = kos_store.count_user_task_packs(task["id"], emp)
        if mine >= task["per_person"]:
            return self._json(400, {"error": f"你已领满 {task['per_person']} 篇"})

        combo = kos_store.pick_combo(task["library_id"])
        if not combo:
            return self._json(409, {"error": "素材已发尽,请联系管理员补充可拼图素材"})

        mats = {m["id"]: m for m in kos_store.list_materials(task["library_id"])}
        try:
            cover_key = mats[combo["cover"]]["cos_key"]
            two_keys = [mats[i]["cos_key"] for i in combo["combo2"]]
            four_keys = [mats[i]["cos_key"] for i in combo["combo4"]]
        except KeyError:
            return self._json(500, {"error": "素材记录缺失,请管理员重新同步素材库"})

        # 拉源图
        try:
            client, bucket = _cos_client()
        except RuntimeError as e:
            return self._json(503, {"error": str(e)})
        tmp = Path(tempfile.mkdtemp(prefix="kospack_"))
        try:
            cover_p = _download(client, bucket, cover_key, str(tmp / ("cover_src" + _ext(cover_key))))
            two_p = [_download(client, bucket, k, str(tmp / f"two_{i}{_ext(k)}")) for i, k in enumerate(two_keys)]
            four_p = [_download(client, bucket, k, str(tmp / f"four_{i}{_ext(k)}")) for i, k in enumerate(four_keys)]
        except Exception as e:
            return self._json(502, {"error": f"COS 拉取素材失败:{e}"})

        # 文案(best-effort)
        copy_json = _make_copy(task["brand"], task["product"])

        # 落库占用(拿到 pack_id 命名成品图)
        pack_id = kos_store.record_pack(task["library_id"], combo, task["id"], user, mine, copy_json)

        # 出图(本地):主图/2合1/4合1 全部裁成 3:4(填满裁切,不翻转)
        KOS_OUT.mkdir(parents=True, exist_ok=True)
        try:
            crop_cover(cover_p, str(KOS_OUT / f"{pack_id}_cover.jpg"))
            stack_vertical(two_p, str(KOS_OUT / f"{pack_id}_two.jpg"))
            grid_2x2(four_p, str(KOS_OUT / f"{pack_id}_four.jpg"))
        except Exception as e:
            return self._json(500, {"error": f"出图失败:{e}"})

        return self._json(200, {
            "ok": True,
            "pack_id": pack_id,
            "post_index": mine + 1,
            "per_person": task["per_person"],
            "images": {
                "cover": _img_url(pack_id, "cover", emp),
                "two": _img_url(pack_id, "two", emp),
                "four": _img_url(pack_id, "four", emp),
            },
            "copy": copy_json,
        })

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        try:
            self.wfile.write(msg.encode("utf-8"))
        except Exception:
            pass
