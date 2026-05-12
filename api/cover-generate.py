"""POST /api/cover-generate

请求体示例:
    photo_base64: data URL 格式的图片
    main_title:   主标题
    subtitle:     副标题
    hua_text:     花字内容(20 字内,可多行)
    style:        风格(种草氛围 / 干货教程 / 促销爆款 / 运动潮感)

多样性策略:
  1. 每个风格预置 8 个 prompt 变体,每次随机抽 3 个
  2. 每个 prompt 注入随机调料(构图/光线/装饰/情绪/字体),从元素池里挑
  3. 每次调豆包都带不同的随机 seed,让模型自带变异
  4. 加反相似指令告诉模型不要走常规套路
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


# ───── 风格 prompt 池(每个风格 8 个变体)─────
# 用单引号字符串避免中英文引号冲突;模板里中文用「」做引用

STYLE_PROMPT_POOLS = {
    '种草氛围': [
        '小红书爆款封面 3:4。真人居家氛围:粉黄暖光、阳光感、客厅或卧室生活场景。主标题「{main_title}」大粗黑字 + 厚白描边,占顶部 1/3。副标「{subtitle}」黑底黄字胶囊贴纸感,微旋转。花字「{hua_text}」右下黄色色块手写感粗黑字。',
        '小红书爆款封面 3:4。产品近景特写,柔和景深虚化,米黄/淡粉底有 grain 颗粒质感。「{main_title}」超大粗黑字 + 厚白描边;「{subtitle}」纯黑小标签;「{hua_text}」亮黄不规则贴纸,微旋转。广告大片质感。',
        '小红书爆款封面 3:4。多元素拼贴感:产品 + 散落的咖啡杯/瑜伽垫/植物等生活道具。米色或浅黄底,手撕纸/拍立得边框装饰。「{main_title}」双色立体描边大字,黑底+黄阴影;「{subtitle}」白底黑字贴纸;「{hua_text}」散落右下,多行手写感小字。',
        '小红书爆款封面 3:4。莫兰迪色调治愈系:奶咖色 / 雾灰粉 / 燕麦白,光线柔和自然。「{main_title}」深棕色粗体优雅大字,带细微高光;「{subtitle}」奶咖色细字签名感;「{hua_text}」白底深棕边圆角胶囊,可多行。整体高级质感、私享生活感。',
        '小红书爆款封面 3:4。30+ 都市精致女性视角:奶油白色背景,部分元素镀金质感。「{main_title}」黑色粗体配金色细描边,优雅大方;「{subtitle}」金色细字小标签;「{hua_text}」金底白字贴纸,小巧精致。整体调性:成熟、精致、克制的种草。',
        '小红书爆款封面 3:4。日系清新氛围:大量留白,米白色背景,有日文/英文衬字暗纹。「{main_title}」黑色粗体 + 圆角字形;「{subtitle}」淡灰色细字,主标题正下方;「{hua_text}」浅蓝/浅粉色贴纸,排版灵动。清新极简日系审美。',
        '小红书爆款封面 3:4。复古胶片感:暖橙偏黄色调,有轻微胶片噪点和泛黄边角。「{main_title}」复古衬线粗体黑字 + 微微倾斜;「{subtitle}」棕色细体英文+中文混排;「{hua_text}」红色或墨绿色胶囊贴纸,做旧感。整体氛围:90 年代杂志封面。',
        '小红书爆款封面 3:4。北欧极简家居感:浅木色 + 白墙 + 绿植元素。「{main_title}」黑色超粗体大字,字符间距开;「{subtitle}」灰绿色细字,微下垂排版;「{hua_text}」白底木边胶囊,自然质感。整体:Ins 风、性冷淡风种草。',
    ],

    '干货教程': [
        '小红书爆款封面 3:4。白底干货笔记风:浅米色纸张质感,蓝色不规则手绘边框环绕。顶部 #标签 小字。「{main_title}」超大粗黑字 + 关键词黄色色块高亮;「{subtitle}」黑色细字主标题下方;产品居中清晰。「{hua_text}」白底黑边圆角胶囊,可多行排列。',
        '小红书爆款封面 3:4。科普答疑信息图风:浅灰白渐变,有方框/网格细节。「{main_title}」超大粗黑字 + 黑色阴影 + 关键词黄下划线;「{subtitle}」深灰小字;产品旁有指示箭头/标注线。底部 3 个白底黑边对勾 ✓ 列表,其中之一显示「{hua_text}」。',
        '小红书爆款封面 3:4。避雷红黑警示风:米黄色背景,有红色感叹号警示元素。「{main_title}」超大粗黑字 + 红色描边或下划线;「{subtitle}」红底白字横条强调;产品有圈圈/箭头标注。「{hua_text}」黄色色块底 + 黑色粗字。',
        '小红书爆款封面 3:4。教程步骤拆解风:白底,左侧标 STEP 1 / STEP 2 / STEP 3 数字大色块,中间产品图。「{main_title}」黑色粗体大字顶部;「{subtitle}」红色细字小标题;「{hua_text}」蓝色圆角小贴纸右下,多行排版灵活。教科书般的清晰布局。',
        '小红书爆款封面 3:4。专业测评风:深灰底 + 银白色金属质感细节。「{main_title}」白色超粗体大字 + 黄色高光关键词;「{subtitle}」银灰色细字;产品有「实测/对比」标注。「{hua_text}」黄黑双色立体贴纸,工程师感。整体:专业、可信、有数据感。',
        '小红书爆款封面 3:4。手绘笔记本风:米黄笔记本背景,有横线/方格,圆珠笔涂鸦感。「{main_title}」黑色手写感粗体 + 黄色马克笔涂抹高亮;「{subtitle}」蓝色细字像签字笔;「{hua_text}」黄色便签条贴纸感,可多行排版。亲切学生时代风。',
        '小红书爆款封面 3:4。Q&A 答疑互动风:粉绿/天蓝清新底色,有大问号装饰。「{main_title}」黑色粗体 + 部分字加问号 / 感叹号;「{subtitle}」白底黑边胶囊;「{hua_text}」白色对话气泡形状,内含小字。整体:轻松活泼,适合 Q&A 类干货。',
        '小红书爆款封面 3:4。极简数据可视风:大量留白,产品居中,周围有简洁数据柱状/百分比图。「{main_title}」黑色超粗体大字;「{subtitle}」纯灰小字;「{hua_text}」深蓝色色块 + 白字,工程美学感。冷静、理性、信息密度大。',
    ],

    '促销爆款': [
        '小红书爆款封面 3:4。大促爆款红黄风:红黄高饱和色块拼贴,「上新」「特惠」英文衬底大字。「{main_title}」黄字 + 红色厚描边 + 黑色斜阴影,极强视觉冲击;「{subtitle}」白底红字胶囊;产品有「爆」字标签;「{hua_text}」红黄相间促销贴纸,可多行。',
        '小红书爆款封面 3:4。限时奢华金色风:深红/暗紫渐变 + 中心放射光线。顶部「限时」白底红字小标签。「{main_title}」金色粗黑大字 + 黑色阴影,有奖杯/皇冠装饰;「{subtitle}」白色小字;「{hua_text}」亮金色胶囊,可多行堆叠。premium 大促感。',
        '小红书爆款封面 3:4。双 11 大促节点:黑红配色,带 NEW / HOT / SALE 英文衬底大字。「{main_title}」立体描边白字 + 红色阴影 + 黄色高亮关键词;「{subtitle}」白色细字;产品被红色圆形框衬托;「{hua_text}」价格爆炸星形贴纸。',
        '小红书爆款封面 3:4。新品上市优雅大促:米色高级背景 + 烫金「NEW ARRIVAL」大字衬底。「{main_title}」深棕色粗体优雅大字 + 金色描边;「{subtitle}」白底深棕色细字胶囊;「{hua_text}」深红色色块 + 烫金字。轻奢、上新季感。',
        '小红书爆款封面 3:4。直播带货风:亮粉/亮黄底,有「直播间」标识 + 倒计时元素。「{main_title}」白色粗体 + 黑色厚描边 + 紧迫感;「{subtitle}」红底白字横条;产品有「主播力荐」贴纸;「{hua_text}」霓虹色块贴纸,堆叠多个。喊麦风。',
        '小红书爆款封面 3:4。折扣价格爆炸风:白底 + 大红价格数字占主视觉。「{main_title}」红黑双色立体描边大字,数字突出显示;「{subtitle}」黑底白字横条;产品旁有「省 XXX 元」贴纸;「{hua_text}」黄底红字爆炸星形,可多行。极强转化导向。',
        '小红书爆款封面 3:4。福利节日喜庆风:中国红 + 烫金,有传统节日元素(灯笼/福字/烟花)。「{main_title}」金色粗黑大字 + 红色描边;「{subtitle}」白底红字胶囊;「{hua_text}」金色圆形章戳贴纸。新春/双 11/618 节点感。',
        '小红书爆款封面 3:4。倒计时紧迫风:暗色背景 + 大数字时钟元素。「{main_title}」白色粗黑字 + 红色阴影,有滴答感;「{subtitle}」黄色小字加粗;「{hua_text}」红底白字胶囊,「仅剩 XX 小时」感。强紧迫、强转化。',
    ],

    '运动潮感': [
        '小红书爆款封面 3:4。健身房力量感:黑色 / 深灰底 + 高对比荧光黄/绿点缀,有动感线条/速度模糊/汗水元素。「{main_title}」超大粗黑字 + 荧光黄描边 + 黑色阴影,运动力量感字体;「{subtitle}」白底黑字小标签,微倾斜;「{hua_text}」荧光黄底 + 黑色粗字胶囊,可多行。',
        '小红书爆款封面 3:4。户外阳光潮酷:蓝天阳光感渐变 + 镜头光晕 lens flare。「{main_title}」超大白字 + 黑色厚描边 + 蓝色阴影;「{subtitle}」黄色斜体小字,主标题旁;产品阳光感色彩鲜艳;「{hua_text}」白底黑字 + 黄色波浪下划线,可多行。',
        '小红书爆款封面 3:4。暗黑燃脂硬核:黑色或暗红渐变 + 铁锈/金属质感。顶部 BURN / POWER / HIIT 英文衬底大字。「{main_title}」白字 + 红色厚描边 + 黑色阴影,粗壮硬朗;「{subtitle}」红色小字加粗;产品金属反光感、汗水滴落;「{hua_text}」红黑双色立体描边,不规则多行。',
        '小红书爆款封面 3:4。运动品牌平面广告风:大胆构图,产品占据画面一半,极简色块背景(单色或双色对比)。「{main_title}」运动品牌字体感,无衬线超粗,微倾斜动感;「{subtitle}」少量装饰,简洁有力;「{hua_text}」对角放置贴纸,Nike / Adidas 平面广告调。',
        '小红书爆款封面 3:4。Y2K 千禧潮酷:亮金属银 / 紫色霓虹 / 全息渐变背景。「{main_title}」金属感粗体字 + 镜面反光 + 紫色阴影;「{subtitle}」霓虹色小字;「{hua_text}」全息贴纸感色块,可多行排列。年轻、潮、未来感。',
        '小红书爆款封面 3:4。瑜伽冥想治愈运动:晨光柔和暖色调,有植物/瑜伽垫/淡色天空。「{main_title}」深棕色或墨绿色粗体优雅大字;「{subtitle}」奶咖色细字签名感;产品在自然光下;「{hua_text}」叶绿色色块 + 白字胶囊,清新自然。',
        '小红书爆款封面 3:4。HIIT 间歇训练动感:对角线动态构图,有运动模糊/速度感线条。「{main_title}」黄色粗体大字 + 黑色厚描边 + 倾斜动感;「{subtitle}」白底黑字尖角横条;产品周围有数据图表元素(心率/卡路里);「{hua_text}」黑底黄字胶囊,数据感。',
        '小红书爆款封面 3:4。撸铁硬汉风:深棕/黑色 + 红色重点,有铁器质感、力量元素。「{main_title}」白色超粗体 + 黑色超厚描边,有「块状」力量感;「{subtitle}」红色小字粗体;产品周围有数据/重量标注;「{hua_text}」红黑双色冲击力贴纸。',
    ],
}

DEFAULT_STYLE = '种草氛围'


# ───── 随机调料元素池(让每次都不一样)─────

SPICE_COMPOSITION = [
    '采用对角线构图,主体偏右上',
    '采用三分法构图,主体在画面下半部黄金分割点',
    '采用中心对称构图',
    '采用左右平衡构图,文字居左,产品居右',
    '采用 C 字形动线引导视线',
    '采用上下分屏布局,上半部文字下半部产品',
    '采用 S 形动线串联各元素',
    '采用满版构图,产品近景占满画面',
    '采用景深前后景层叠构图,前景虚化引导',
]

SPICE_LIGHTING = [
    '光线参考:柔和的清晨晨光,暖色调',
    '光线参考:午后明亮硬光,高对比',
    '光线参考:侧逆光,产品边缘有光晕',
    '光线参考:夕阳余晖,金黄色调',
    '光线参考:阴天柔光,色调清冷',
    '光线参考:摄影棚白光,均匀通透',
    '光线参考:夜景霓虹色温,有色彩反射',
    '光线参考:窗边自然光,温暖斑驳',
]

SPICE_DECORATION = [
    '装饰加分项:加入手绘箭头标注关键产品部位',
    '装饰加分项:加入圆形高光圈聚焦某个细节',
    '装饰加分项:加入便利贴贴纸点缀',
    '装饰加分项:加入波浪下划线强调关键词',
    '装饰加分项:加入小型对话气泡装饰',
    '装饰加分项:加入星星/勾选符号小图标',
    '装饰加分项:加入印章/盖戳元素',
    '装饰加分项:加入丝带/标签贴纸点缀',
    '装饰加分项:加入手绘涂鸦边框',
]

SPICE_MOOD = [
    '整体情绪:平静治愈、宁静',
    '整体情绪:活力满满、energetic',
    '整体情绪:都市精致、轻奢质感',
    '整体情绪:复古怀旧、暖意',
    '整体情绪:清新自然、文艺感',
    '整体情绪:科技感、未来感',
    '整体情绪:温暖治愈、家的感觉',
    '整体情绪:酷感、潮流感',
]

SPICE_TYPOGRAPHY = [
    '字体细节:主标题字符之间适度紧凑,有压迫感',
    '字体细节:主标题字符间距宽松,呼吸感',
    '字体细节:主标题轻微倾斜约 5 度,动感',
    '字体细节:主标题全部居中规整,稳重',
    '字体细节:关键词单独加大字号,其他字号小',
    '字体细节:多字体混排,主副标题字体形态不同',
]


# 通用文字渲染要求(所有 prompt 末尾拼接)
TEXT_REQUIREMENTS = (
    '\n\n【文字渲染要求 — 极其重要】\n'
    '1. 中文文字必须清晰可读、字形完整、一字不差,严格按上述提供的文字内容渲染\n'
    '2. 主标题字号最大,副标题中等,花字小贴纸,3 个层级要明显\n'
    '3. 文字与产品/场景不重叠,有清晰图层关系\n'
    '4. 字体:粗体中文 sans-serif,有白色描边或黑色阴影增强可读性\n'
    '5. 如果花字内容较长(>10 字),允许自然换行成 2-3 行,排版灵活、活泼但整齐'
)

# 反相似指令(强制每次出图不要走常规套路)
ANTI_REPEAT = (
    '\n\n【避免雷同 — 重要】\n'
    '不要套用常规小红书封面套路,本次必须有独特的视觉特征。'
    '构图、配色细节、装饰元素都要与其他常见封面有所差异化。'
)


def compose_prompt(base_template: str, main_title: str, subtitle: str, hua_text: str) -> str:
    """组装 prompt:基础模板 + 随机调料 + 文字要求 + 反相似指令"""
    spices = [
        random.choice(SPICE_COMPOSITION),
        random.choice(SPICE_LIGHTING),
        random.choice(SPICE_DECORATION),
        random.choice(SPICE_MOOD),
        random.choice(SPICE_TYPOGRAPHY),
    ]
    spice_block = '\n\n【本次额外要求(随机变化以避免雷同)】\n' + '\n'.join(f'- {s}' for s in spices)
    full = base_template + spice_block + TEXT_REQUIREMENTS + ANTI_REPEAT
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
    """同步调用一次豆包,带随机 seed 增强变异性"""
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

            # 从 8 个模板里随机抽 3 个,每个 prompt 注入随机调料
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
