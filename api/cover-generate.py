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


DOUBAO_URL = 'https://ark.cn-beijing.volces.com/api/v3/images/generations'
DOUBAO_MODEL = os.environ.get('DOUBAO_MODEL', 'doubao-seedream-5-0-lite-260128')
SIZE = os.environ.get('DOUBAO_IMAGE_SIZE', '1920x2560')
MAX_PHOTO_BYTES = 4 * 1024 * 1024


# ═════════════ 风格模板池(每风格 5 个差异化变体)═════════════
# 每个变体加入:具体配色(色名/Pantone)、构图参考、质感关键词、英文加分项

STYLE_PROMPT_POOLS = {
    '种草氛围': [
        # ① 真人晨光生活感
        '小红书爆款种草封面 3:4。视觉风格:Living Lifestyle Photography。'
        '画面:暖色调晨光感家居场景(米色 #F5E6D3 / 蜜桃粉 #FFD4B8 / 奶咖色 #E8C8A8),'
        '柔和侧光从窗外漏进,有 grain 颗粒质感。产品作为画面右侧主体,左侧留出文字空间。'
        '主标题「{main_title}」用粗黑体超大字号,白色厚描边 4px,黑色 drop shadow 8px,占顶部 1/3。'
        '副标「{subtitle}」黑底黄字胶囊贴纸,微旋转 -3 度,有贴纸质感。'
        '花字「{hua_text}」右下黄色不规则色块底,粗黑手写感字。'
        '审美参考:Vogue 居家板块、小红书"精致生活"博主风格、Casetify lifestyle 广告。',

        # ② 产品 hero 特写
        '小红书爆款种草封面 3:4。视觉风格:Hero Product Shot。'
        '画面:产品近景占据画面 65%,背景柔和景深虚化(米黄 #F4E4C1 或淡粉 #F5DBDB),'
        '有专业摄影的高光和阴影层次,光线参考:摄影棚柔光箱 + 反光板。'
        '主标题「{main_title}」超大粗黑字 + 白色厚描边 5px,占据画面顶部约 25% 区域。'
        '副标「{subtitle}」纯黑细字小标签贴在主标题侧边。'
        '花字「{hua_text}」亮黄 #FFD42D 不规则贴纸 + 微旋转,放在画面右下角。'
        '审美参考:Apple 产品摄影、Aesop 极简广告、高级杂志大片。',

        # ③ 拼贴生活感
        '小红书爆款种草封面 3:4。视觉风格:Lifestyle Collage / Magazine Layout。'
        '画面:产品居中,周围散落生活道具(咖啡杯、瑜伽垫、绿植、运动袜),'
        '米色 #EFE6D5 背景,有手撕纸 / 拍立得 / 胶带 拼贴元素。'
        '主标题「{main_title}」双色立体描边大字:黑字底 + 黄色 #FFD42D 偏移阴影,'
        '占据顶部,可微倾斜增加动感。'
        '副标「{subtitle}」白底黑字贴纸,有图钉装饰。'
        '花字「{hua_text}」散落画面右下角,多行手写感小字,可加波浪下划线。'
        '审美参考:Vogue 拼贴页、Pinterest mood board、ZINE 杂志。',

        # ④ 莫兰迪治愈
        '小红书爆款种草封面 3:4。视觉风格:Morandi Color Palette / 治愈系。'
        '画面:奶咖色 #C9B0A0 / 雾灰粉 #D6B8B8 / 燕麦白 #E8DDD0 配色,'
        '柔和自然光,无强对比,情绪安静私密。产品作为画面主体,留白多。'
        '主标题「{main_title}」深棕色 #5C4A3C 粗体优雅大字,有细微高光反射。'
        '副标「{subtitle}」奶咖色细字签名感,排版微下垂。'
        '花字「{hua_text}」白底深棕边圆角胶囊,可多行,清新雅致。'
        '审美参考:Hermes 广告、KKW Beauty 包装、北欧家居杂志。',

        # ⑤ 日系清新留白
        '小红书爆款种草封面 3:4。视觉风格:Japanese Minimalism / Muji 风。'
        '画面:大量留白,米白色 #F8F4ED 背景,有日文/英文衬字暗纹做装饰底。'
        '产品作为画面下半部主体,克制不堆砌。'
        '主标题「{main_title}」黑色粗体 + 圆角字形,有呼吸感的字符间距。'
        '副标「{subtitle}」淡灰色 #999 细字,主标题正下方。'
        '花字「{hua_text}」浅蓝 #A8C8E8 或浅粉 #F4C8C8 不规则贴纸,排版灵动。'
        '审美参考:Muji 广告、Kinfolk 杂志、日本设计大师作品。',
    ],

    '干货教程': [
        # ① 白底笔记风
        '小红书爆款干货封面 3:4。视觉风格:Handwritten Notebook / Knowledge Card。'
        '画面:浅米色 #FAF6EC 纸张质感背景,蓝色 #1A6BCF 不规则手绘边框环绕(像马克笔涂的)。'
        '顶部小字 # 标签。'
        '主标题「{main_title}」超大粗黑字,**关键词**用黄色 #FFD42D 色块高亮圈出。'
        '副标「{subtitle}」黑色细字主标题下方。'
        '产品居中清晰,无干扰元素。'
        '花字「{hua_text}」白底黑边圆角胶囊,可多行排列。'
        '审美参考:学霸笔记博主、爆款干货封面、Notion 模板。',

        # ② 信息图表科普风
        '小红书爆款干货封面 3:4。视觉风格:Infographic / Data Visualization。'
        '画面:浅灰白 #F5F5F7 渐变背景,有方框 / 网格 / 标注线细节。'
        '主标题「{main_title}」超大粗黑字 + 黑色阴影,关键词加黄色下划线。'
        '副标「{subtitle}」深灰小字。'
        '产品有指示箭头和标注线,营造"专业评测"感。'
        '底部 3 个白底黑边对勾 ✓ 列表,其中之一显示「{hua_text}」。'
        '审美参考:Apple 产品页面、科技测评 KOL 封面、TED talk 海报。',

        # ③ 避雷警示风
        '小红书爆款干货封面 3:4。视觉风格:Warning Poster / Caution Sign。'
        '画面:米黄色 #FFF4D0 背景,有红色感叹号 / 警示标记元素。'
        '主标题「{main_title}」超大粗黑字 + 红色 #E53935 描边或下划线,有警示感。'
        '副标「{subtitle}」红底白字横条强调,占据画面中部。'
        '产品有圈圈 / 箭头标注重点位置。'
        '花字「{hua_text}」黄色色块 + 黑色粗字,角标位置。'
        '审美参考:消费者权益公益海报、避雷帖博主封面。',

        # ④ 教程步骤拆解风
        '小红书爆款干货封面 3:4。视觉风格:Step-by-Step Tutorial / 教科书布局。'
        '画面:白色 #FFFFFF 背景,左侧 STEP 1 / STEP 2 / STEP 3 大数字色块(蓝 #2563EB / 黄 #F59E0B / 红 #DC2626),中间产品图清晰。'
        '主标题「{main_title}」黑色粗体大字顶部,占据画面 1/4。'
        '副标「{subtitle}」红色细字小标题。'
        '花字「{hua_text}」蓝色圆角小贴纸右下,可多行,排版灵活。'
        '审美参考:IKEA 安装说明、教育博主封面、Pinterest DIY 教程。',

        # ⑤ Q&A 答疑互动风
        '小红书爆款干货封面 3:4。视觉风格:Q&A Conversation / Speech Bubble。'
        '画面:粉绿 #A8E8D4 / 天蓝 #B4D8F5 清新底色拼接,有大问号「?」装饰元素。'
        '主标题「{main_title}」黑色粗体,部分字加问号 / 感叹号 emphasize。'
        '副标「{subtitle}」白底黑边胶囊形状,像"答案"。'
        '花字「{hua_text}」白色对话气泡形状内含小字,可多行排版。'
        '产品周围有思考 / 提问 doodle 涂鸦。'
        '审美参考:Duolingo 营销、Q&A 类知识博主封面、Pixar 卡通。',
    ],

    '促销爆款': [
        # ① 大促爆款红黄
        '小红书爆款促销封面 3:4。视觉风格:Aggressive Sale Banner。'
        '画面:红黄高饱和色块拼贴背景:大红 #E53935 + 亮黄 #FFD42D + 黑色 #1A1A1A,'
        '带「上新」「特惠」英文 NEW / SALE 衬底大字。'
        '主标题「{main_title}」黄字 + 红色厚描边 5px + 黑色斜阴影,极强视觉冲击,占据画面顶部 35%。'
        '副标「{subtitle}」白底红字胶囊,有"爆"字角标。'
        '产品居中,有红色圆形/星形框衬托。'
        '花字「{hua_text}」红黄相间促销贴纸,可多行堆叠。'
        '审美参考:淘宝双 11 大促 banner、Black Friday 海报。',

        # ② 限时奢华金色
        '小红书爆款促销封面 3:4。视觉风格:Premium Limited Edition / Luxury Sale。'
        '画面:深红 #8B0000 / 暗紫 #4A1F4F 渐变背景 + 中心放射状金色光线效果。'
        '顶部「限时」白底红字小标签。'
        '主标题「{main_title}」金色 #D4AF37 粗黑大字 + 黑色阴影,有奖杯/皇冠装饰元素。'
        '副标「{subtitle}」白色小字,主标题下方。'
        '产品被金色光晕环绕。'
        '花字「{hua_text}」亮金色胶囊,可多行堆叠。'
        '审美参考:奢侈品大促海报、SK-II 限时活动、TIFFANY 节日营销。',

        # ③ 双 11 节点风
        '小红书爆款促销封面 3:4。视觉风格:Singles Day / Black Friday Campaign。'
        '画面:黑红配色 (#1A1A1A + #E53935),带 NEW / HOT / SALE 英文衬底大字。'
        '主标题「{main_title}」立体描边白字 + 红色阴影 + 黄色 #FFD42D 高亮关键词。'
        '副标「{subtitle}」白色细字,排版有动感。'
        '产品被红色圆形/星形框衬托,周围有爆炸星形装饰。'
        '花字「{hua_text}」价格爆炸贴纸,可多行,有透视感。'
        '审美参考:天猫双 11 主视觉、Amazon Prime Day 海报、Nike Sale 广告。',

        # ④ 新品上市优雅
        '小红书爆款促销封面 3:4。视觉风格:Elegant New Arrival / Premium Launch。'
        '画面:米色 #F4E4C1 高级背景 + 烫金「NEW ARRIVAL」英文大字衬底。'
        '主标题「{main_title}」深棕色 #5C4A3C 粗体优雅大字 + 金色 #D4AF37 描边。'
        '副标「{subtitle}」白底深棕色细字胶囊,排版精致。'
        '产品居中,有柔和高光照明。'
        '花字「{hua_text}」深红 #8B0000 色块 + 烫金字,角标位置。'
        '审美参考:Burberry 新品发布、轻奢品牌上新页。',

        # ⑤ 折扣价格爆炸
        '小红书爆款促销封面 3:4。视觉风格:Price Slash / Crazy Discount。'
        '画面:白色 #FFFFFF 底 + 大红价格数字占主视觉(模拟"省 XXX 元"或"-50%")。'
        '主标题「{main_title}」红黑双色立体描边大字,数字突出显示。'
        '副标「{subtitle}」黑底白字横条强调。'
        '产品旁有"省 XX 元"或"直降"星形贴纸。'
        '花字「{hua_text}」黄底红字爆炸星形,可多行,极强转化导向。'
        '审美参考:京东大促价签、奥莱 outlet banner、价格爆炸营销海报。',
    ],
}

DEFAULT_STYLE = '种草氛围'


# ═════════════ 随机调料元素池(增强多样性)═════════════

SPICE_COMPOSITION = [
    '采用对角线构图,主体偏右上,有动感',
    '采用三分法构图,主体在画面下半部黄金分割点',
    '采用中心对称构图,稳重平衡',
    '采用左右平衡构图,文字居左,产品居右',
    '采用 C 字形动线引导视线',
    '采用上下分屏布局,上半部文字下半部产品',
    '采用 S 形动线串联各元素',
    '采用满版构图,产品近景占满画面',
    '采用景深前后景层叠构图,前景虚化引导',
]

SPICE_LIGHTING = [
    '光线:柔和的清晨晨光,色温 4000K,暖金色调',
    '光线:午后明亮硬光,色温 5500K,高对比',
    '光线:摄影棚柔光箱 + 反光板,均匀通透',
    '光线:夕阳余晖,色温 3000K,温暖金黄',
    '光线:窗边自然光,色温 5000K,柔和斑驳',
    '光线:戏剧性侧逆光,产品边缘有 rim light 光晕',
    '光线:阴天柔光,色温偏冷 6000K,情绪沉静',
    '光线:霓虹色温混合光,有色彩反射,夜晚氛围',
]

SPICE_DECORATION = [
    '装饰加分项:加入手绘箭头标注关键产品部位',
    '装饰加分项:加入圆形高光圈聚焦某个细节',
    '装饰加分项:加入便利贴贴纸点缀(微旋转贴纸感)',
    '装饰加分项:加入波浪下划线强调关键词',
    '装饰加分项:加入小型对话气泡装饰',
    '装饰加分项:加入星星 ★ / 勾选 ✓ 符号小图标',
    '装饰加分项:加入印章 / 盖戳元素',
    '装饰加分项:加入丝带 / 飘带 / 标签贴纸点缀',
    '装饰加分项:加入手绘涂鸦边框,像马克笔即兴画的',
]

SPICE_MOOD = [
    '整体情绪:平静治愈、宁静细腻',
    '整体情绪:活力满满 energetic、年轻态度',
    '整体情绪:都市精致、轻奢质感',
    '整体情绪:复古怀旧、暖意 vintage',
    '整体情绪:清新自然、文艺感',
    '整体情绪:科技感、未来感、酷感',
    '整体情绪:温暖治愈、家的感觉',
    '整体情绪:潮流前卫、街头感',
]

SPICE_TYPOGRAPHY = [
    '字体细节:主标题字符之间紧凑,有压迫感',
    '字体细节:主标题字符间距宽松,呼吸感',
    '字体细节:主标题轻微倾斜约 5 度,动感',
    '字体细节:主标题全部居中规整,稳重',
    '字体细节:关键词单独加大字号,层次清晰',
    '字体细节:多字体混排,主副标题字体形态不同',
]


# ═════════════ 通用渲染要求(末尾拼接)═════════════

TEXT_REQUIREMENTS = (
    '\n\n【文字渲染要求 — 严格执行】\n'
    '1. 中文文字**必须清晰可读、字形完整、一字不差**,严格按上述提供的文字内容渲染,不得增删改字\n'
    '2. 主标题字号最大,副标题中等,花字小贴纸,3 个层级要明显区分\n'
    '3. 文字与产品/场景不重叠,有清晰图层关系\n'
    '4. 字体:粗体中文 sans-serif(类似思源黑体或阿里巴巴普惠体 Heavy),有白色描边或黑色阴影增强可读性\n'
    '5. 花字内容较长(>10 字)时,自然换行成 2-3 行,排版灵活整齐'
)

ANTI_REPEAT = (
    '\n\n【避免雷同 — 重要】\n'
    '本次出图必须有独特视觉特征,构图、配色细节、装饰元素都要避免常规小红书封面套路。'
    '强调差异化,让这张图在 100 张同类封面里能被一眼认出。'
)

QUALITY_BOOST = (
    '\n\n【整体出图要求】\n'
    '- 商业海报级精度,trending on Xiaohongshu cover, professional graphic design, ad campaign quality\n'
    '- 色彩饱和度恰到好处、不过曝、有质感\n'
    '- 文字排版精致,有"被设计师精心调过版"的感觉'
)


def compose_prompt(base_template: str, main_title: str, subtitle: str, hua_text: str) -> str:
    """组装 prompt:基础模板 + 5 类随机调料 + 文字 + 反相似 + 质量"""
    spices = [
        random.choice(SPICE_COMPOSITION),
        random.choice(SPICE_LIGHTING),
        random.choice(SPICE_DECORATION),
        random.choice(SPICE_MOOD),
        random.choice(SPICE_TYPOGRAPHY),
    ]
    spice_block = '\n\n【本次随机变化要求】\n' + '\n'.join(f'- {s}' for s in spices)
    full = base_template + spice_block + TEXT_REQUIREMENTS + ANTI_REPEAT + QUALITY_BOOST
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


def call_seededit(prompt, image_data_url, results, idx, seed):
    api_key = os.environ.get('DOUBAO_API_KEY', '').strip()
    if not api_key:
        results[idx] = {'error': 'DOUBAO_API_KEY 未配置'}
        return

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
        with urllib.request.urlopen(req, timeout=55) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            items = data.get('data', [])
            if items and items[0].get('url'):
                results[idx] = {'url': items[0]['url']}
            else:
                results[idx] = {'error': f'豆包返回结构异常: {data}'}
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='ignore') if e.fp else ''
        results[idx] = {'error': f'豆包 API 错误 {e.code}: {detail[:300]}'}
    except Exception as e:
        results[idx] = {'error': f'调用异常: {e}'}


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
            style = (req.get('style') or '').strip() or DEFAULT_STYLE

            user = req.get('_user') or {}
            print(
                f"[USAGE] action=cover_generate "
                f"user={user.get('emp_id')}/{user.get('name')}/{user.get('department')} "
                f"style={style} title={main_title[:30]}",
                flush=True,
            )
            try:
                import sys as _sys
                from pathlib import Path as _Path
                _sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
                from lib.kv_store import log_event
                log_event('cover_generate', user, {'style': style, 'title': main_title[:30]})
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
            for t in threads:
                t.join(timeout=58)

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
