"""POST /api/cover-generate

第二轮优化版 prompt:在保持 3 风格 × 5 变体的精简结构基础上,
为每个变体加入具体配色、构图参考、质感关键词;恢复 SPICE_LIGHTING/MOOD
+ 反相似指令,显著提升出图质感。

请求体示例:
    photo_base64: data URL
    main_title / subtitle / hua_text: 文字内容
    style: 种草氛围 / 干货教程 / 促销爆款
"""
from http.server import BaseHTTPRequestHandler
import os
import json
import base64
import re
import random
import urllib.request
import urllib.error
import threading
import sys as _sys_boot
from pathlib import Path as _Path_boot
_ROOT_BOOT = _Path_boot(__file__).resolve().parent.parent
if str(_ROOT_BOOT) not in _sys_boot.path:
    _sys_boot.path.insert(0, str(_ROOT_BOOT))


DOUBAO_URL = 'https://ark.cn-beijing.volces.com/api/v3/images/generations'
DOUBAO_MODEL = os.environ.get('DOUBAO_MODEL', 'doubao-seedream-4-5-251128')
SIZE = os.environ.get('DOUBAO_IMAGE_SIZE', '1920x2560')
MAX_PHOTO_BYTES = 4 * 1024 * 1024


# ═════════════ 强力保留原图前缀(每个 prompt 都贴这段)═════════════

PRESERVE_PHOTO = (
    '【第一优先级 · 保持照片主体】\n'
    '本图是用户上传的真实产品照片,请在这张照片基础上做小红书封面设计。\n'
    '产品的形状、结构、比例、颜色、角度,以及人物的五官与姿态,必须与原图保持一致,'
    '不要替换成其它产品或人物,不要凭空改变产品外观或型号。\n'
    '允许:整体调色与加滤镜(通透奶油、清新胶片、日系等)、适度光影与暗角、'
    '以及在产品四周叠加设计元素(色块、线条、几何形状、贴纸、光斑)来提升封面质感。\n'
    '\n'
)


# ═════════════ 版式风格池(设计语言,契合 Seedream 4.5 版式思维)═════════════
# 每个池随机抽 3 套版式并发出图;模板只描述配色/滤镜/版式骨架,
# 具体三级文字内容与"一字不差"约束由 TEXT_REQUIREMENTS 统一负责。

STYLE_PROMPT_POOLS = {
    '种草氛围': [
        '【版式 · 治愈清新 冷蓝调】\n'
        '- 整体清新冷蓝色调滤镜,画面通透,顶部/底部轻微暗角聚焦\n'
        '- 主标题「{main_title}」置于顶部偏左,超大粗黑体,白色描边、黑色字,处于视觉中心\n'
        '- 副标题「{subtitle}」紧随主标题下方,小一号白色字\n'
        '- 花字正文「{hua_text}」置于底部,浅蓝立体感粗体作点缀\n'
        '- 角落可加简单几何色块或细线条,不遮挡产品\n',

        '【版式 · 奶油暖橙 治愈风】\n'
        '- 奶油暖橙滤镜,温暖治愈氛围\n'
        '- 主标题「{main_title}」顶部偏左,超大粗黑体,米白描边、深棕字\n'
        '- 副标题「{subtitle}」主标下方,深棕小字,可两行\n'
        '- 花字正文「{hua_text}」底部偏上,暖橙立体粗体点缀\n'
        '- 可加手写感小元素或圆点装饰,不遮挡产品\n',

        '【版式 · 莫兰迪高级灰 极简】\n'
        '- 低饱和莫兰迪色调,简约高级\n'
        '- 主标题「{main_title}」上部居中或偏左,粗黑体,深色字配细描边\n'
        '- 副标题「{subtitle}」主标附近,灰调小字\n'
        '- 花字正文「{hua_text}」以小色块卡片承载,置于一角\n'
        '- 细线条分隔、留白充足,整体干净\n',

        '【版式 · 日系胶片 文艺风】\n'
        '- 淡淡胶片颗粒与柔光滤镜,复古文艺\n'
        '- 主标题「{main_title}」上部,粗黑体带轻微手写感,清晰描边\n'
        '- 副标题「{subtitle}」主标下方细字\n'
        '- 花字正文「{hua_text}」斜向排布点缀,复古配色\n'
        '- 可加胶片边框或柔光线条元素(不含真实文字)\n',

        '【版式 · 清新薄荷 活力风】\n'
        '- 薄荷绿与奶白配色,清新有活力\n'
        '- 主标题「{main_title}」中上部,超大粗黑体,白描边\n'
        '- 副标题「{subtitle}」放入圆角色块卡片中\n'
        '- 花字正文「{hua_text}」放入另一个撞色圆角色块点缀\n'
        '- 加少量圆点/星形小元素,不遮挡产品\n',
    ],
    '干货教程': [
        '【版式 · 知识卡片 清爽风】\n'
        '- 顶部横幅色块承载主标题,白底黑字清晰专业\n'
        '- 主标题「{main_title}」超大粗黑体,关键词可加大\n'
        '- 副标题「{subtitle}」横幅下方小字\n'
        '- 花字正文「{hua_text}」以要点标签/胶囊形式排布\n'
        '- 干净留白,轻微投影增强层次\n',

        '【版式 · 蓝白专业 信息栏】\n'
        '- 蓝白配色,专业清晰\n'
        '- 主标题「{main_title}」加粗置于上部\n'
        '- 副标题「{subtitle}」小字补充\n'
        '- 花字正文「{hua_text}」以序号/要点形式列出\n'
        '- 细线条与图标感元素点缀(不含真实文字)\n',

        '【版式 · 手账便签 亲和风】\n'
        '- 手账便签风,加胶带、便利贴、手绘小元素,暖色纸张质感滤镜\n'
        '- 主标题「{main_title}」马克笔粗体,醒目\n'
        '- 副标题「{subtitle}」便签内小字\n'
        '- 花字正文「{hua_text}」以贴纸/圈注形式点缀\n',

        '【版式 · 极简高对比】\n'
        '- 大面积留白,高对比黑白灰\n'
        '- 主标题「{main_title}」超大居中,极粗黑体\n'
        '- 副标题「{subtitle}」主标下方细字\n'
        '- 花字正文「{hua_text}」角落小字点缀\n'
        '- 一条强调色线条或色块提亮\n',
    ],
    '促销爆款': [
        '【版式 · 红黄撞色 强促销】\n'
        '- 红黄撞色,热闹强冲击\n'
        '- 主标题「{main_title}」超大红字白描边,极醒目\n'
        '- 副标题「{subtitle}」深色底白字横条强调\n'
        '- 花字正文「{hua_text}」放入爆炸星形贴纸,突出优惠\n'
        '- 促销元素置于四周,不遮挡产品主体\n',

        '【版式 · 直降风暴 深色底】\n'
        '- 深色底配亮色,强转化\n'
        '- 主标题「{main_title}」亮色超大粗体\n'
        '- 副标题「{subtitle}」亮色小字\n'
        '- 花字正文「{hua_text}」多行堆叠贴纸,转化导向\n'
        '- 星形/爆炸贴纸与光效点缀\n',

        '【版式 · 限时抢购 橙红渐变】\n'
        '- 橙红渐变,限时紧迫感\n'
        '- 主标题「{main_title}」微倾斜超大粗体,动感十足\n'
        '- 副标题「{subtitle}」胶囊色块内白字\n'
        '- 花字正文「{hua_text}」角标贴纸强调\n'
        '- 加闪电/光效等紧迫感设计元素(不含真实文字)\n',

        '【版式 · 好物种草+优惠 混合风】\n'
        '- 清爽底 + 局部亮色优惠标签\n'
        '- 主标题「{main_title}」超大粗黑体白描边\n'
        '- 副标题「{subtitle}」小字补充卖点\n'
        '- 花字正文「{hua_text}」放入圆角优惠贴纸\n'
        '- 整体通透不杂乱,优惠信息醒目\n',
    ],
}

DEFAULT_STYLE = '种草氛围'


COPY_TYPE_TO_STYLE = {
    '种草': '种草氛围',
    '场景': '种草氛围',
    '干货': '干货教程',
    '促销': '促销爆款',
}


def map_copy_type_to_style(copy_type: str) -> str:
    """文案类型 → 封面风格名;落到对应风格池"""
    return COPY_TYPE_TO_STYLE.get((copy_type or '').strip(), DEFAULT_STYLE)


TEXT_REQUIREMENTS = (
    '\n【封面文字 · 三级层级(最重要,必须一字不差)】\n'
    '严格按上面版式中给出的主标题、副标题、花字正文内容渲染,'
    '不得增字、漏字、改字、写错别字、生造字,也不得替换成拼音或英文:\n'
    '  · 主标题:最大、最醒目,是整张封面的视觉中心\n'
    '  · 副标题:次一级,位于主标题附近作补充\n'
    '  · 花字正文:装饰性短句,作点缀\n'
    '字号层级:主标题 > 花字正文 > 副标题,对比明显,一眼看清主标题。\n'
    '字体:粗壮中文黑体(bold sans-serif),笔画完整清晰、边缘锐利、不糊不断不变形。\n'
    '若某字段显示为"(无副标题)"或"(无花字)",则该级文字不渲染。\n'
    '【严禁】画面中除上述三级文字外,不得出现任何其它文字、英文单词、品牌名、'
    '产品型号、数字水印、二维码或 AI 签名——这些极易出错,一律不要出现。\n'
)

QUALITY_BOOST = (
    '\n【风格与质感】\n'
    '- 小红书爆款封面级审美,像专业设计师精心排版\n'
    '- 竖版 3:4 构图,主体突出,文字与画面主次分明\n'
    '- 适度滤镜与统一色调,画面通透高级、有质感\n'
    '- 干净、无水印、无杂乱多余文字\n'
)


def compose_prompt(base_template: str, main_title: str, subtitle: str, hua_text: str) -> str:
    """组装 prompt:强力保留原图前缀 + 版式描述 + 文字要求 + 品质收尾

    注意:删除了 SPICE_* 调料池,因为"配色/光线/构图"那类描述会引导
    模型重画产品,与本次"只叠加文字图层"的核心目标冲突。
    """
    full = (
        PRESERVE_PHOTO
        + base_template
        + TEXT_REQUIREMENTS
        + QUALITY_BOOST
    )
    return full.format(
        main_title=main_title,
        subtitle=subtitle or '(无副标题)',
        hua_text=hua_text or '(无花字)',
    )


def parse_data_url(data_url):
    m = re.match(r'data:([^;]+);base64,(.+)$', data_url.strip(), re.DOTALL)
    if not m:
        return 'image/jpeg', base64.b64decode(data_url)
    return m.group(1), base64.b64decode(m.group(2))


HTTP_TIMEOUT = int(os.environ.get('DOUBAO_HTTP_TIMEOUT', '90'))  # 单次 HTTP 超时(秒)


def _call_doubao_once(prompt, image_data_url, seed, api_key):
    """单次调用,返回 (url, error_str, is_timeout)"""
    payload = {
        'model': DOUBAO_MODEL,
        'prompt': prompt,
        'image': image_data_url,
        'size': SIZE,
        'response_format': 'url',
        'watermark': False,
        'seed': seed,
    }
    req = urllib.request.Request(
        DOUBAO_URL,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            items = data.get('data', [])
            if items and items[0].get('url'):
                return items[0]['url'], None, False
            return None, f'豆包返回结构异常: {str(data)[:200]}', False
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='ignore') if e.fp else ''
        # 4xx/5xx,快速失败,允许重试
        return None, f'HTTP {e.code}: {detail[:300]}', False
    except Exception as e:
        is_timeout = 'timeout' in str(e).lower() or isinstance(e, TimeoutError)
        return None, f'调用异常 ({type(e).__name__}): {e}', is_timeout


def call_seededit(prompt, image_data_url, results, idx, seed):
    """带重试的封装:
    - 快速失败(HTTP 4xx/5xx / 结构异常):换一个 seed 立刻重试 1 次
    - 超时失败:不重试(避免业务方等待时间翻倍)
    """
    api_key = os.environ.get('DOUBAO_API_KEY', '').strip()
    if not api_key:
        results[idx] = {'error': 'DOUBAO_API_KEY 未配置'}
        return

    url, err, is_timeout = _call_doubao_once(prompt, image_data_url, seed, api_key)
    if url:
        results[idx] = {'url': url, 'attempt': 1}
        return

    print(f"[cover_generate] idx={idx} 第 1 次失败:{err[:200]}", flush=True)

    if is_timeout:
        # 超时不重试,直接返回错误
        results[idx] = {'error': f'豆包出图超时({HTTP_TIMEOUT}s):{err}'}
        return

    # 快速失败,换 seed 重试一次
    new_seed = random.randint(1, 2**31 - 1)
    url2, err2, _ = _call_doubao_once(prompt, image_data_url, new_seed, api_key)
    if url2:
        results[idx] = {'url': url2, 'attempt': 2}
        return
    print(f"[cover_generate] idx={idx} 第 2 次仍失败:{err2[:200]}", flush=True)
    results[idx] = {'error': f'重试后仍失败 — 第1次: {err} / 第2次: {err2}'}


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length).decode('utf-8') if length else '{}'
            req = json.loads(body)

            photo_b64 = req.get('photo_base64') or ''
            main_title = (req.get('main_title') or '').strip()
            subtitle = (req.get('subtitle') or '').strip()
            hua_text = (req.get('hua_text') or '').strip()

            # 优先用 copy_type 自动映射风格(新版),否则用旧的 style 参数(向后兼容)
            copy_type = (req.get('copy_type') or '').strip()
            if copy_type:
                style = map_copy_type_to_style(copy_type)
            else:
                style = (req.get('style') or '').strip() or DEFAULT_STYLE

            from lib.session import user_from_headers
            user = user_from_headers(self.headers)
            if not user:
                return self._json(401, {"error": "未登录或登录已过期,请重新登录"})
            from lib.ratelimit import check as _rl_check
            _ok, _msg = _rl_check(user, self.client_address[0] if self.client_address else '', 'cover_generate')
            if not _ok:
                return self._json(429, {"error": _msg})
            print(
                f"[USAGE] action=cover_generate "
                f"user={user.get('emp_id')}/{user.get('name')}/{user.get('department')} "
                f"copy_type={copy_type} style={style} title={main_title[:30]}",
                flush=True,
            )
            try:
                import sys as _sys
                from pathlib import Path as _Path
                _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
                from lib.kv_store import log_event
                log_event('cover_generate', user, {
                    'copy_type': copy_type,
                    'style': style,
                    'title': main_title[:30],
                })
            except Exception:
                pass

            if not photo_b64:
                return self._json(400, {'error': '请上传产品照片'})
            if not main_title:
                return self._json(400, {'error': '主标题不能为空'})
            if style not in STYLE_PROMPT_POOLS:
                return self._json(400, {'error': f'未知风格:{style}'})

            try:
                mime, photo_bytes = parse_data_url(photo_b64)
            except Exception as e:
                return self._json(400, {'error': f'图片格式无法解析:{e}'})

            if len(photo_bytes) > MAX_PHOTO_BYTES:
                return self._json(413, {
                    'error': f'图片过大({len(photo_bytes)//1024//1024}MB),请压缩到 3MB 内'
                })

            m = re.match(r'data:[^;]+;base64,(.+)$', photo_b64.strip(), re.DOTALL)
            photo_base64_clean = m.group(1) if m else photo_b64.strip()
            image_data_url = f'data:{mime};base64,{photo_base64_clean}'

            # 从 5 个模板里随机抽 3 个,每个 prompt 注入 5 类随机调料
            pool = STYLE_PROMPT_POOLS[style]
            chosen_templates = random.sample(pool, k=min(3, len(pool)))
            prompts = [
                compose_prompt(t, main_title, subtitle, hua_text)
                for t in chosen_templates
            ]
            seeds = [random.randint(1, 2**31 - 1) for _ in range(3)]

            results = [None, None, None]
            threads = []
            for i in range(3):
                t = threading.Thread(
                    target=call_seededit,
                    args=(prompts[i], image_data_url, results, i, seeds[i]),
                )
                t.start()
                threads.append(t)
            # 最坏情况:首次 timeout(HTTP_TIMEOUT)后超时不重试,直接返回
            # 或首次快速失败(<5s)+ 重试一次(HTTP_TIMEOUT)→ 也大致 HTTP_TIMEOUT + 5s
            # 主线程等待略放宽,允许 thread 自己处理重试逻辑
            join_timeout = HTTP_TIMEOUT * 2 + 10
            for t in threads:
                t.join(timeout=join_timeout)

            images = [r['url'] for r in results if r and 'url' in r]
            errors = [r['error'] for r in results if r and 'error' in r]

            if not images:
                return self._json(502, {
                    'error': '3 张都生成失败',
                    'details': errors,
                })

            self._json(200, {'images': images, 'errors': errors})

        except Exception as e:
            self._json(500, {'error': str(e)})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)
