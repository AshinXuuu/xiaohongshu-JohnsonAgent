"""KOS 业务侧接口。

POST /api/kos
  action=my_tasks                     我的待办任务 + 进度
  action=issue   {task_id}            领一份素材包:挑唯一组合 → COS 拉源图 → 拼图 → 文案
                                        → 本地出图 → 返回 3 图(带令牌的下载地址)+ 文案
  action=complete {pack_id, note_url} 回填小红书链接 → 标记已发布
  action=leaderboard                  排行榜(完成任务数 + 笔记数)
  action=my_packs {task_id?}          我领过的记录
  action=ai_cover {pack_id}           可选 AI 封面:文案快照提炼三段字 → 豆包在原封面素材上
                                        叠字出图,返回候选图临时 URL(不落库、不改 pack)
  action=regen_copy {pack_id}         重新生成本篇文案(领取时 AI 超时导致空文案时用),回填 pack

GET /api/kos?img=1&pack=<id>&kind=cover|two|four&t=<token>
  令牌校验后返回本地成品 JPEG(成品图不写 COS,规避只读密钥限制)。
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import os
import sys
import json
import base64
import hashlib
import random
import shutil
import tempfile
import threading
import mimetypes
import importlib.util

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from lib import kos_store, products_store
from lib.auth import is_admin
from lib.image_compose import stack_vertical, grid_2x2, crop_cover

KOS_OUT = ROOT / 'data' / 'kos_out'
COPY_TYPE = '种草'
_IMG_EXT = ('.jpg', '.jpeg', '.png', '.webp')

import hmac


def _img_secret() -> bytes:
    """成品图下载令牌的签名密钥。
    去掉了原先的硬编码兜底('kos-johnson-img-secret')—— 那是可预测值,
    加上 pack_id(自增)、emp_id(可枚举)后任何人都能离线算出令牌、越权下载他人成品图。
    现在优先读专用密钥 KOS_IMG_SECRET,回退到已有的高熵 SESSION_SECRET;都没有则直接报错,
    绝不使用可预测的常量。"""
    s = (os.environ.get('KOS_IMG_SECRET')
         or os.environ.get('SESSION_SECRET')
         or os.environ.get('DEEPSEEK_API_KEY'))
    if not s:
        raise RuntimeError('KOS 图片令牌密钥未配置,请在 .env 设置 KOS_IMG_SECRET')
    return s.encode('utf-8')


def _ext(key):
    e = os.path.splitext(key)[1].lower()
    return e if e in _IMG_EXT else '.jpg'


def _token(pack_id, emp_id):
    # 完整 HMAC-SHA256(不再截断),消息与密钥分离
    msg = f"{pack_id}:{emp_id}".encode('utf-8')
    return hmac.new(_img_secret(), msg, hashlib.sha256).hexdigest()


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


def _fields_module():
    spec = importlib.util.spec_from_file_location('kos_fieldsmod', str(ROOT / 'api' / 'cover-fields.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _cover_gen_module():
    spec = importlib.util.spec_from_file_location('kos_covergenmod', str(ROOT / 'api' / 'cover-generate.py'))
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


# 多维随机池:人设 × 开头钩子 × 文体结构 × 语气,与角度池组合,
# 让同一产品的每篇文案在"谁在说、怎么开头、什么结构、什么腔调"上都不同
_PERSONAS = [
    "久坐腰酸的上班族", "在家带娃的宝妈", "第一次买健身器材的新手小白",
    "减脂期的年轻女生", "想练出线条的增肌期男生", "备考久坐的学生党",
    "开始发福的中年上班族", "不爱去健身房的社恐", "倒班作息不规律的打工人",
    "给爸妈买器材的子女", "健身房老手转居家训练", "经常出差、健身不连续的人",
]
_HOOK_TYPES = [
    "自嘲吐槽式开头:先讲自己的懒惰/失败经历再引出产品",
    "悬念反转式开头:先抑后扬(注意:严禁使用『踩雷』『晾衣架』这两个被用滥的字眼)",
    "场景描写式开头:直接把读者带进一个具体画面(时间、地点、动作)",
    "数字变化式开头:用一个具体的数字或身体变化开场",
    "对话引用式开头:引用家人/朋友/同事说的一句原话",
    "自问自答式开头:抛出一个目标人群最关心的问题,然后自己回答",
]
_STRUCTURES = [
    "时间线日记体:按『第1周/第2周/现在』的时间节奏推进",
    "清单体:把体验拆成几条,用 emoji 序号逐条讲",
    "对比体:围绕『以前 vs 现在』或『买前预期 vs 实际体验』展开",
    "踩坑复盘体:先讲选购时的纠结和对比,再讲为什么最后选了它",
    "问答体:围绕 2-3 个读者最关心的问题组织正文",
    "碎碎念随笔体:松散但真实的口语流水账,想到哪说到哪",
]
_TONES = [
    "元气活泼,感叹和 emoji 偏多", "理性冷静,像专业测评博主",
    "幽默自嘲,会开自己的玩笑", "温柔细腻,重感受描写",
    "直来直去大白话,不绕弯子",
]


# 固定标签批次:每次领取的标签都一样(品牌 + 产品 + 一组通用种草标签),不随 AI 变动、不会缺失
_KOS_FIXED_TAGS = ["#居家健身", "#健身好物", "#科学健身", "#自律生活", "#运动日常", "#健身打卡"]


def _fixed_tags(brand_name, product_name):
    return [f"#{brand_name}", f"#{product_name}"] + _KOS_FIXED_TAGS


def _make_copy(brand_name, product_name):
    """标题+正文:每人各不相同(高随机 + 随机种草角度 + 变体编号,失败重试);
    标签:固定同一批(品牌 + 产品 + 通用标签),永不缺失、每次一致。"""
    tags = _fixed_tags(brand_name, product_name)
    try:
        gen = _gen_module()
        brand, product = products_store.find_product(brand_name, product_name)
        if not product:
            return {"titles": [], "body": "", "tags": tags}
        angle = random.choice(_COPY_ANGLES)
        persona = random.choice(_PERSONAS)
        hook = random.choice(_HOOK_TYPES)
        structure = random.choice(_STRUCTURES)
        tone = random.choice(_TONES)
        n_points = random.choice([2, 2, 3])   # 多数只写 2 个卖点,少数 3 个
        nonce = random.randint(1000, 9999)
        extra = (
            f"本篇的写作设定(必须全部执行,这是和其他篇区分的关键):\n"
            f"1. 人设:你是一位{persona},全文视角、用词、场景都要贴合这个身份;\n"
            f"2. 切入角度:{angle};\n"
            f"3. 开头钩子:{hook};\n"
            f"4. 文体结构:{structure};\n"
            f"5. 语气:{tone};\n"
            f"6. 卖点取舍:只从产品资料中挑 {n_points} 个最贴合上述人设的卖点重点展开,"
            f"其余卖点最多一句带过或干脆不提,严禁逐条罗列全部卖点;\n"
            f"7. 标题也要贴合上述人设与语气,不要用其他人设的口吻。\n"
            f"(变体编号 {nonce},仅用于强制区分,不要写进文案)")
        sysp = gen.load_prompt(COPY_TYPE)
        userp = gen.build_user_message(brand, product, COPY_TYPE, extra)
        raw = None
        for _ in range(2):        # 失败重试一次,避免返回空文案(空文案会显得"重复/缺失")
            try:
                raw = gen.call_deepseek(sysp, userp, temperature=1.05)
                break
            except Exception:
                continue
        if not raw:
            return {"titles": [], "body": "", "tags": tags}
        parsed = gen.parse_model_output(raw)
        titles = [gen.clean_inline(t) for t in (parsed.get("titles") or [])
                  if isinstance(t, str) and t.strip()]
        body = gen.normalize_body(parsed.get("body", "") or "")
        return {"titles": titles, "body": body, "tags": tags}   # 标签强制用固定批次
    except Exception:
        return {"titles": [], "body": "", "tags": tags}


def _img_url(pack_id, kind, emp_id):
    return f"/api/kos?img=1&pack={pack_id}&kind={kind}&t={_token(pack_id, emp_id)}"


# ──────────────── 可选 AI 封面(不落库、不改 pack)────────────────

def _ai_cover_n():
    """一次生成几张候选封面:环境变量 KOS_AI_COVER_N 可调,限 1-3,默认 2。"""
    try:
        n = int(os.environ.get('KOS_AI_COVER_N', '2'))
    except ValueError:
        n = 2
    return max(1, min(3, n))


def _photo_data_url(path, max_w=1200, quality=85):
    """底图预处理:旋正 → 宽压到 ≤ max_w 的 JPG → base64 data url
    (cover-generate 接收的就是 data url,压缩后稳在其 4MB 上限内)。"""
    from io import BytesIO
    from PIL import Image, ImageOps
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img).convert('RGB')
        if img.width > max_w:
            img = img.resize((max_w, round(img.height * max_w / img.width)), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, 'JPEG', quality=quality)
    return 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')


def _ai_cover_fields(pack, brand, product):
    """三段封面字:复用 cover-fields 的提炼逻辑(extract_fields),
    输入 pack 的文案快照 —— 标题取第一条 + 正文 + 标签;copy_json 为空时
    就自然落到 cover-fields 的「按产品资料」路径。
    AIBusyError 向上抛(调用方回 503);其余失败兜底为产品名/品牌名,保证仍能出图。"""
    try:
        copy_json = json.loads(pack.get('copy_json') or '{}') or {}
    except Exception:
        copy_json = {}
    titles = [t for t in (copy_json.get('titles') or []) if isinstance(t, str) and t.strip()]
    body = (copy_json.get('body') or '').strip()
    tags = [t for t in (copy_json.get('tags') or []) if isinstance(t, str)]
    from lib.aigate import AIBusyError
    try:
        fm = _fields_module()
        return fm.extract_fields(
            brand, product, copy_type=COPY_TYPE,
            existing_titles=titles[:1], existing_body=body, existing_tags=tags)
    except AIBusyError:
        raise
    except Exception:
        import traceback; traceback.print_exc()
        return {"main_title": (product or "好物分享")[:12], "subtitle": brand or "", "hua_text": ""}


def _gen_ai_covers(fields, image_data_url, n):
    """复用 cover-generate 的生成通路(版式模板池 + compose_prompt + call_seededit
    自带的失败重试),并发出 n 张,返回豆包临时图片 URL 列表(全失败为空)。"""
    cg = _cover_gen_module()
    style = cg.map_copy_type_to_style(COPY_TYPE)
    pool = cg.STYLE_PROMPT_POOLS[style]
    templates = random.sample(pool, k=min(n, len(pool)))
    prompts = [cg.compose_prompt(t, fields.get('main_title') or '',
                                 fields.get('subtitle') or '', fields.get('hua_text') or '')
               for t in templates]
    results = [None] * len(prompts)
    threads = []
    for i, p in enumerate(prompts):
        t = threading.Thread(target=cg.call_seededit,
                             args=(p, image_data_url, results, i, random.randint(1, 2**31 - 1)))
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=cg.HTTP_TIMEOUT * 2 + 10)
    return [r['url'] for r in results if r and 'url' in r]


def kos_ai_cover(pack_id, emp, rl_check=None):
    """action=ai_cover 的核心流程,返回 (http_code, 响应 dict)。
    抽成模块级函数便于离线单测;rl_check 由 handler 传入限流闭包,
    顺序保持「归属校验 → 限流」,越权请求不消耗配额。"""
    pack = kos_store.get_pack(pack_id)
    if not pack:
        return 404, {"error": "素材包不存在"}
    if (pack.get('emp_id') or '') != emp:
        return 403, {"error": "无权操作他人的素材包"}
    if rl_check is not None:
        ok, msg = rl_check()
        if not ok:
            return 429, {"error": msg}

    task = kos_store.get_task(pack.get('task_id')) or {}
    fields = _ai_cover_fields(pack, task.get('brand') or '', task.get('product') or '')

    # 底图:领取时的封面素材 → COS 拉原图 → 压成 data url
    mats = {m['id']: m for m in kos_store.list_materials(pack['library_id'])}
    mat = mats.get(pack.get('cover_material_id'))
    if not mat:
        return 500, {"error": "素材记录缺失,请管理员重新同步素材库"}
    try:
        client, bucket = _cos_client()
    except RuntimeError as e:
        return 503, {"error": str(e)}
    tmp = Path(tempfile.mkdtemp(prefix="kosai_"))
    try:
        key = mat['cos_key']
        src = _download(client, bucket, key, str(tmp / ('cover_src' + _ext(key))))
        image_data_url = _photo_data_url(src)
    except Exception as e:
        print(f"[KOS] ai_cover 底图准备失败:{e}", flush=True)
        return 502, {"error": "AI 封面生成失败,请稍后重试(不影响已领取的图文)"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    covers = _gen_ai_covers(fields, image_data_url, _ai_cover_n())
    if not covers:
        return 502, {"error": "AI 封面生成失败,请稍后重试(不影响已领取的图文)"}
    # 持久化:把豆包临时 URL 下载到本地成品目录,业务以后每次打开素材包都能看到
    covers = _persist_ai_covers(pack['id'], covers, emp)
    return 200, {"ok": True, "covers": covers, "fields": fields}


def _persist_ai_covers(pack_id, urls, emp):
    """AI 封面落地:下载到 KOS_OUT/{pack}_ai{i}.jpg(与素材包三图同目录同令牌机制),
    返回带令牌的本地地址;单张下载失败该张退回临时 URL(仍可看,只是不持久)。
    每次「再来一批」覆盖上一批,磁盘上每个 pack 至多 3 张。"""
    import urllib.request
    KOS_OUT.mkdir(parents=True, exist_ok=True)
    for old in KOS_OUT.glob(f"{pack_id}_ai*.jpg"):
        try:
            old.unlink()
        except OSError:
            pass
    out = []
    for i, u in enumerate(urls[:3]):
        try:
            with urllib.request.urlopen(u, timeout=60) as resp:
                data = resp.read()
            fp = KOS_OUT / f"{pack_id}_ai{i}.jpg"
            fp.write_bytes(data)
            # URL 带上文件版本号(mtime),「再来一批」覆盖后 URL 变化 → 浏览器不吃旧缓存
            out.append(_img_url(pack_id, f"ai{i}", emp) + f"&v={int(fp.stat().st_mtime)}")
        except Exception as e:
            print(f"[KOS] AI 封面持久化失败(第{i+1}张,退回临时地址):{e}", flush=True)
            out.append(u)
    return out


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
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
                if kind not in ('cover', 'two', 'four', 'ai0', 'ai1', 'ai2'):
                    return self._err(400, "bad kind")
                pack = kos_store.get_pack(pack_id)
                if not pack or not hmac.compare_digest(tok, _token(pack['id'], pack['emp_id'])):
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
            import traceback; traceback.print_exc()
            print("[API-500] " + getattr(self, "path", "") + " " + repr(e), flush=True)
            self._err(500, "服务器开小差了,请稍后重试")

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

            # 是否参与 KOS 任务(世代主管等岗位在后台勾掉后:不派任务、不能领取;
            # 自发布登记 / 排行 / 查看已领记录不受影响)
            def _kos_joined():
                try:
                    from lib.users_store import get_user as _gu
                    rec = _gu(user.get("department"), user.get("name"), emp)
                    return (rec or {}).get("kos_join", True)
                except Exception:
                    return True   # 查不到时不拦(fail-open,避免误伤)

            if action == "my_tasks":
                if not _kos_joined():
                    return self._json(200, {"tasks": [], "kos_exempt": True})
                return self._json(200, {"tasks": kos_store.tasks_for_user(user)})

            if action == "summary":
                return self._json(200, kos_store.my_kos_summary(user))

            if action == "leaderboard":
                return self._json(200, {"board": kos_store.leaderboard()})

            if action == "my_packs":
                # 富化后返回,供任意设备恢复「已领素材包」显示(数据早已在服务器,零新增存储)
                raw = kos_store.my_packs(emp, req.get("task_id"))
                task_cache = {}
                packs = []
                for p in raw:
                    tid = p.get("task_id")
                    if tid not in task_cache:
                        task_cache[tid] = kos_store.get_task(tid) or {} if tid else {}
                    t = task_cache[tid]
                    pid = p["id"]
                    ai = []
                    for i in range(3):
                        _af = KOS_OUT / f"{pid}_ai{i}.jpg"
                        if _af.exists():
                            ai.append(_img_url(pid, f"ai{i}", emp) + f"&v={int(_af.stat().st_mtime)}")
                    try:
                        copy_json = json.loads(p.get("copy_json") or "{}") or {}
                    except Exception:
                        copy_json = {}
                    packs.append({
                        "pack_id": pid, "task_id": tid,
                        "post_index": p.get("post_index") or 1,
                        "per_person": (t.get("per_person") if t else None) or 1,
                        "task_name": ((t.get("title") if t else "") or
                                      ((t.get("brand") or "") + " " + (t.get("product") or "")).strip()
                                      if t else "任务") or "任务",
                        "task_open": (t.get("status") == "open") if t else False,
                        "note_url": p.get("note_url") or "",
                        "at": (p.get("created_at") or 0) * 1000,
                        "images": {k: _img_url(pid, k, emp) for k in ("cover", "two", "four")},
                        "copy": copy_json,
                        "ai_covers": ai,
                    })
                return self._json(200, {"packs": packs})

            if action == "my_self_posts":
                return self._json(200, {"posts": kos_store.my_self_posts(emp)})

            if action == "self_publish":
                res = kos_store.add_self_post(user, req.get("note_url") or "")
                if res == 'bad_url':
                    return self._json(400, {"error": "链接无效,请粘贴正确的小红书笔记链接。"})
                return self._json(200, {"ok": True})

            if action == "delete_self_post":
                ok = kos_store.delete_self_post(req.get("id"), emp)
                if not ok:
                    return self._json(400, {"error": "删除失败(记录不存在或非本人)"})
                return self._json(200, {"ok": True})

            if action == "complete":
                res = kos_store.publish_pack(req.get("pack_id"), emp, req.get("note_url") or "")
                if res == 'bad_url':
                    return self._json(400, {"error": "链接无效,请粘贴正确的小红书笔记链接。"})
                if res == 'not_owner' or res is False:
                    return self._json(400, {"error": "回填失败(记录不存在或非本人)"})
                return self._json(200, {"ok": True})

            if action == "issue":
                if not _kos_joined():
                    return self._json(403, {"error": "你的账号未参与 KOS 任务,如有疑问请联系管理员"})
                return self._issue(req, user, emp)

            if action == "ai_cover":
                # 付费接口:与封面生成共用 'cover_generate' 配额池(限流在归属校验之后执行)
                from lib.ratelimit import check as _rl_check
                from lib.aigate import AIBusyError
                ip = self.client_address[0] if self.client_address else ''
                print(f"[USAGE] action=kos_ai_cover user={emp}/{user.get('name')}/{user.get('department')} "
                      f"pack={req.get('pack_id')}", flush=True)
                try:
                    code, obj = kos_ai_cover(
                        req.get("pack_id"), emp,
                        rl_check=lambda: _rl_check(user, ip, 'cover_generate'))
                except AIBusyError as e:
                    return self._json(503, {"error": str(e)})
                return self._json(code, obj)

            if action == "regen_copy":
                # 重新生成本篇文案(领取时 AI 超时/失败导致标题正文为空时用)。
                # 归属校验 → 限流(与文案生成同 'generate' 配额池)→ 重生成 → 回填 pack。
                pack = kos_store.get_pack(req.get("pack_id"))
                if not pack:
                    return self._json(404, {"error": "素材包不存在"})
                if (pack.get("emp_id") or "") != emp:
                    return self._json(403, {"error": "无权操作他人的素材包"})
                from lib.ratelimit import check as _rl_check
                ip = self.client_address[0] if self.client_address else ''
                ok, msg = _rl_check(user, ip, 'generate')
                if not ok:
                    return self._json(429, {"error": msg})
                task = kos_store.get_task(pack.get("task_id")) or {}
                brand = task.get("brand") or ""
                product = task.get("product") or ""
                copy_json = _make_copy(brand, product)
                if not (copy_json.get("titles") or copy_json.get("body")):
                    # AI 仍未吐出正文,不覆盖旧文案,让用户再试
                    return self._json(502, {"error": "文案生成失败,请稍后再试一次(标签不受影响)"})
                kos_store.set_pack_copy(pack["id"], copy_json)
                print(f"[USAGE] action=kos_regen_copy user={emp}/{user.get('name')} pack={pack['id']}", flush=True)
                return self._json(200, {"ok": True, "copy": copy_json})

            return self._json(400, {"error": "未知 action"})
        except Exception as e:
            import traceback; traceback.print_exc()
            print("[API-500] " + getattr(self, "path", "") + " " + repr(e), flush=True)
            self._json(500, {"error": "服务器开小差了,请稍后重试"})

    def _issue(self, req, user, emp):
        task = kos_store.get_task(req.get("task_id"))
        if not task or task.get("status") != "open":
            return self._json(404, {"error": "任务不存在或已结束"})
        if task["scope"] == "dept" and user.get("department") not in (task.get("depts") or []):
            return self._json(403, {"error": "该任务不在你的部门范围内"})

        # 原子领取(2026-07 并发修复):限额校验 + 选组合 + 落库在同一事务,
        # 并发领取不会拿到重复组合、双击不会超领。
        # 定向补发:该人该任务的额外配额计入上限。
        _limit = int(task["per_person"]) + kos_store.get_task_bonus(task["id"], emp)
        status, data = kos_store.claim_pack(
            task["library_id"], task["id"], user, _limit)
        if status == 'limit':
            return self._json(400, {"error": f"你已领满 {_limit} 篇"})
        if status == 'exhausted':
            return self._json(409, {"error": "素材已发尽,请联系管理员补充可拼图素材"})
        if status != 'ok':
            print(f"[KOS] claim_pack 失败:{data}", flush=True)
            return self._json(500, {"error": "领取失败,请稍后重试"})
        pack_id, combo, mine = data["pack_id"], data["combo"], data["post_index"]

        def _fail(code, msg):
            # 领取已占用组合,后续任一步失败都要回滚释放(否则组合被白白烧掉)
            try:
                kos_store.delete_pack(pack_id)
            except Exception:
                pass
            return self._json(code, {"error": msg})

        mats = {m["id"]: m for m in kos_store.list_materials(task["library_id"])}
        try:
            cover_key = mats[combo["cover"]]["cos_key"]
            two_keys = [mats[i]["cos_key"] for i in combo["combo2"]]
            four_keys = [mats[i]["cos_key"] for i in combo["combo4"]]
        except KeyError:
            return _fail(500, "素材记录缺失,请管理员重新同步素材库")

        # 拉源图
        try:
            client, bucket = _cos_client()
        except RuntimeError as e:
            return _fail(503, str(e))
        tmp = Path(tempfile.mkdtemp(prefix="kospack_"))
        try:
            cover_p = _download(client, bucket, cover_key, str(tmp / ("cover_src" + _ext(cover_key))))
            two_p = [_download(client, bucket, k, str(tmp / f"two_{i}{_ext(k)}")) for i, k in enumerate(two_keys)]
            four_p = [_download(client, bucket, k, str(tmp / f"four_{i}{_ext(k)}")) for i, k in enumerate(four_keys)]
            # 组合按 sorted id 存库(唯一性),拼接位置随机:近/远景上下随机、田字四格随机
            random.shuffle(two_p)
            random.shuffle(four_p)
        except Exception as e:
            return _fail(502, f"COS 拉取素材失败:{e}")

        # 文案(best-effort,在事务外生成后回填,避免 LLM 调用拖长写锁)
        copy_json = _make_copy(task["brand"], task["product"])
        try:
            kos_store.set_pack_copy(pack_id, copy_json)
        except Exception:
            pass  # 文案快照回填失败不影响领取

        # 出图(本地):主图/2合1/4合1 全部裁成 3:4(填满裁切,不翻转)
        KOS_OUT.mkdir(parents=True, exist_ok=True)
        try:
            crop_cover(cover_p, str(KOS_OUT / f"{pack_id}_cover.jpg"))
            stack_vertical(two_p, str(KOS_OUT / f"{pack_id}_two.jpg"))
            grid_2x2(four_p, str(KOS_OUT / f"{pack_id}_four.jpg"))
        except Exception as e:
            import traceback; traceback.print_exc()
            return _fail(500, "出图失败,请重试;若反复失败请联系管理员检查素材图片")

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
