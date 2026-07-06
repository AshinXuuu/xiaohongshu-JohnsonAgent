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
    '【第一优先级 · 保持照片原样】\n'
    '本图是用户上传的真实产品照片。请在这张照片上叠加小红书爆款封面文字与少量为文字服务的设计元素,'
    '照片本身保持原样:不要加滤镜、不要整体调色、不要改变亮度色温、不要重绘或替换产品与人物,'
    '产品的形状、结构、颜色、角度以及人物五官姿态都与原图一致。\n'
    '只在画面上叠加:三级封面文字,以及少量衬托文字的设计元素(色块、圆角卡片、贴纸、箭头、描边),'
    '这些元素不要遮挡产品主体与关键细节。\n'
    '\n'
)


# ═════════════ 文字版式池(每套一个鲜明配色方案 + 排列 + 关键词高亮策略)═════════════
# 每个池随机抽 3 套并发出图;模板负责"配色/字形/描边/排列/高亮"的差异,
# 不改动照片;文字内容与"一字不差·简体·爆款工艺"由 TEXT_REQUIREMENTS 统一负责。

STYLE_PROMPT_POOLS = {
    '种草氛围': [
        '【配色 · 白黄经典爆款】\n'
        '- 主标题「{main_title}」纯白超粗黑体 + 黑色厚描边,顶部横向铺开,关键词可放大\n'
        '- 副标题「{subtitle}」亮黄色粗体 + 黑色描边,紧随主标下方,与主标白黄撞色\n'
        '- 花字正文「{hua_text}」白字黑描边,置于底部一侧\n'
        '- 可加小箭头/圆点点缀,不遮挡产品\n',

        '【配色 · 亮绿描边活力】\n'
        '- 主标题「{main_title}」亮绿色超粗黑体 + 白色描边 + 深色投影,顶部\n'
        '- 副标题「{subtitle}」白色粗体 + 黑描边,主标下方\n'
        '- 花字正文「{hua_text}」亮黄字黑描边,底部点缀\n'
        '- 关键词可单独放大,整体醒目有活力\n',

        '【配色 · 白橙暖撞色】\n'
        '- 主标题「{main_title}」纯白超粗黑体 + 黑厚描边,上部\n'
        '- 副标题「{subtitle}」橙红色粗体 + 白描边,与主标撞色\n'
        '- 花字正文「{hua_text}」白字黑描边,底部\n'
        '- 暖色系小元素点缀,不遮挡产品\n',

        '【配色 · 撞色色块卡片】\n'
        '- 主标题「{main_title}」白色超粗黑体 + 黑描边,上部\n'
        '- 副标题「{subtitle}」放入亮黄圆角色块,块内黑字\n'
        '- 花字正文「{hua_text}」放入蓝色或绿色圆角色块,块内白字\n'
        '- 色块与文字撞色鲜明,活泼吸睛\n',

        '【配色 · 黑白高级+一抹亮色】\n'
        '- 主标题「{main_title}」纯黑或纯白超大极粗黑体,顶部\n'
        '- 把主标题里最关键的词单独换成一抹亮色(亮黄/红/亮绿)高亮\n'
        '- 副标题「{subtitle}」中性灰细字,主标下方\n'
        '- 花字正文「{hua_text}」放入胶囊贴纸点缀,高对比有设计感\n',

        '【配色 · 粉白少女感】\n'
        '- 主标题「{main_title}」纯白超粗黑体 + 粉色厚描边,上部,甜美吸睛\n'
        '- 副标题「{subtitle}」粉红色粗体 + 白描边,主标下方\n'
        '- 花字正文「{hua_text}」放入粉底白字圆角胶囊\n'
        '- 可加爱心/星星小元素点缀,不遮挡产品\n',

        '【配色 · 黑金高级】\n'
        '- 主标题「{main_title}」香槟金/暖金色超粗黑体 + 黑色描边,顶部,质感高级\n'
        '- 副标题「{subtitle}」纯白粗字 + 黑描边,主标下方\n'
        '- 花字正文「{hua_text}」金色字黑描边,底部点缀\n'
        '- 深色细线条或金色小元素点缀,不遮挡产品\n',

        '【配色 · 奶茶大地色】\n'
        '- 主标题「{main_title}」焦糖棕超粗黑体 + 米白厚描边,上部,温柔高级\n'
        '- 副标题「{subtitle}」奶油色粗字 + 深棕描边,主标附近\n'
        '- 花字正文「{hua_text}」大地色系,放入米白圆角色块\n'
        '- 低饱和暖调点缀元素,不遮挡产品\n',
    ],
    '干货教程': [
        '【配色 · 白黄知识风】\n'
        '- 顶部横幅色块,主标题「{main_title}」白字 + 黑描边,关键数字放大变亮黄\n'
        '- 副标题「{subtitle}」白字黑描边,横幅下方\n'
        '- 花字正文「{hua_text}」以亮色要点胶囊排布\n'
        '- 干净专业,层级分明\n',

        '【配色 · 蓝白专业】\n'
        '- 主标题「{main_title}」深蓝超粗黑体 + 白描边,上部\n'
        '- 副标题「{subtitle}」深灰粗字,主标附近\n'
        '- 花字正文「{hua_text}」蓝底白字胶囊,一角\n'
        '- 细线条点缀,专业清晰\n',

        '【配色 · 绿黑燃脂风】\n'
        '- 主标题「{main_title}」亮绿超粗黑体 + 白描边 + 黑投影,顶部\n'
        '- 副标题「{subtitle}」白字黑描边,主标下方\n'
        '- 花字正文「{hua_text}」中的关键词用亮黄高亮\n'
        '- 强烈运动感,醒目\n',

        '【配色 · 便签手账】\n'
        '- 主标题「{main_title}」马克笔手写感粗体(黑或红),醒目\n'
        '- 副标题「{subtitle}」手写小字\n'
        '- 花字正文「{hua_text}」以亮色圈注/贴纸点缀\n'
        '- 胶带、便利贴等手账小元素,不遮挡产品\n',

        '【配色 · 蓝紫渐变清爽】\n'
        '- 主标题「{main_title}」蓝紫渐变超粗黑体 + 白色描边,上部,清爽有科技感\n'
        '- 副标题「{subtitle}」深灰粗字,主标下方\n'
        '- 花字正文「{hua_text}」蓝紫底白字胶囊,一角\n'
        '- 细线条与图标感元素点缀(不含真实文字)\n',
    ],
    '促销爆款': [
        '【配色 · 红黄撞色强促销】\n'
        '- 主标题「{main_title}」大红超粗黑体 + 白厚描边,极醒目\n'
        '- 副标题「{subtitle}」亮黄粗字 + 黑描边,横条强调\n'
        '- 花字正文「{hua_text}」放入爆炸星形贴纸,优惠词/数字放大\n'
        '- 促销元素置于四周,不遮挡产品\n',

        '【配色 · 深底亮价】\n'
        '- 主标题「{main_title}」纯白超粗黑体 + 黑描边\n'
        '- 价格或关键数字用亮黄超大突出\n'
        '- 副标题「{subtitle}」亮色小字\n'
        '- 花字正文「{hua_text}」红色贴纸,转化导向\n',

        '【配色 · 橙红动感】\n'
        '- 主标题「{main_title}」白字 + 橙红厚描边,微倾斜超大,动感十足\n'
        '- 副标题「{subtitle}」胶囊色块内白字\n'
        '- 花字正文「{hua_text}」角标贴纸,关键词放大\n'
        '- 闪电/光效等紧迫感元素点缀\n',

        '【配色 · 好物+优惠】\n'
        '- 主标题「{main_title}」白字 + 黑厚描边,大气\n'
        '- 把优惠相关的词单独用红色高亮\n'
        '- 副标题「{subtitle}」小字补充卖点\n'
        '- 花字正文「{hua_text}」放入亮色圆角优惠贴纸\n',

        '【配色 · 荧光撞色劲爆】\n'
        '- 主标题「{main_title}」荧光黄超粗黑体 + 黑色厚描边,极度吸睛\n'
        '- 副标题「{subtitle}」荧光粉或荧光青粗字 + 黑描边,与主标强撞色\n'
        '- 花字正文「{hua_text}」黑底荧光字贴纸,优惠词/数字放大\n'
        '- 强冲击力,促销元素置于四周,不遮挡产品\n',
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
    '\n【封面标题工艺 · 小红书爆款风(重要)】\n'
    '- 字体:超粗黑体(类似阿里巴巴普惠体 Heavy / 站酷庆科黄油体),方正饱满、笔画厚重\n'
    '- 描边与立体:文字带厚描边(白字配黑边,或亮色字配白边/黑边)+ 轻微立体投影,在照片上也清晰醒目\n'
    '- 多色分层:主标题、副标题、花字正文用不同颜色形成对比(常见白+亮黄、白+亮绿、白+橙红、黑+一抹亮色),不要三级都同一颜色\n'
    '- 关键词高亮:可把主标题里最关键的词或数字单独放大或换成高亮色(亮黄/亮绿/红),制造视觉重点\n'
    '\n【封面文字 · 三级层级(必须一字不差)】\n'
    '严格按上面版式给出的主标题、副标题、花字正文内容渲染,不得增字、漏字、改字、写错别字、生造字,'
    '也不得替换成拼音或英文:\n'
    '  · 主标题:最大、最醒目,视觉中心\n'
    '  · 副标题:次一级,主标题附近补充\n'
    '  · 花字正文:装饰性短句,点缀\n'
    '字号层级:主标题 > 花字正文 > 副标题,对比明显,一眼看清主标题。\n'
    '所有文字必须是规范的简体中文,笔画完整清晰、边缘锐利、不糊不断不变形;严禁繁体字、异体字、错别字。\n'
    '若某字段显示为"(无副标题)"或"(无花字)",则该级文字不渲染。\n'
    '【严禁】画面中除上述三级文字外,不得出现任何其它文字、英文单词、品牌名、产品型号、数字水印、'
    '二维码或 AI 签名——这些极易出错,一律不要出现。\n'
)

QUALITY_BOOST = (
    '\n【风格与质感】\n'
    '- 小红书爆款封面级审美,标题排版像专业设计师精心设计,有冲击力\n'
    '- 竖版 3:4 构图,产品主体清晰,文字主次分明、醒目好读\n'
    '- 照片保持自然原貌(不加滤镜),视觉重点在三级文字的配色与设计感\n'
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
        from lib.aigate import gate as _ai_gate
        with _ai_gate(), urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
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
            import traceback; traceback.print_exc()
            print("[API-500] " + getattr(self, "path", "") + " " + repr(e), flush=True)
            self._json(500, {"error": "服务器开小差了,请稍后重试"})

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(body)
