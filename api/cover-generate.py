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


# ═════════════ 强力保留原图前缀(每个 prompt 都贴这段)═════════════

PRESERVE_PHOTO = (
    '【最高优先级 · 绝对遵守】\n'
    '本图是用户提供的真实产品照片。\n'
    '产品的形状、颜色、材质、角度、表面 logo、型号字符、品牌标识、配件细节,\n'
    '以及人物姿态、衣着、面部、背景物体结构,**必须像素级保留**,\n'
    '严禁重新生成、严禁改变、严禁替换、严禁微调。\n'
    '\n'
    '【你的任务】仅在原图之上**叠加**以下小红书封面排版元素:\n'
    '  - 主标题、副标题、立体描边花字\n'
    '  - 品牌徽章 / 账号水印 / 色块卡片 / 贴纸\n'
    '  - 可选:画面顶部和/或底部局部半透明深色暗角(只压暗,不改色)\n'
    '\n'
    '【严格禁止】\n'
    '  ✗ 重新绘制产品本身的任何部分(包括 logo / 型号 / 屏幕 / 旋钮 / 按键 / 配件)\n'
    '  ✗ 改变产品的角度、颜色、形状、纹理\n'
    '  ✗ 改变背景物体(家具 / 窗户 / 地板 / 墙面 / 植物)的位置和形态\n'
    '  ✗ 向原图场景中添加新的实物(贴纸/色块/文字之外的物体一律不要加)\n'
    '  ✗ 改变光线方向或重新打光\n'
    '\n'
)


# ═════════════ 风格模板池(15 个变体直接复刻 7 张爆款封面)═════════════

STYLE_PROMPT_POOLS = {
    '种草氛围': [
        # A1 · 治愈系跑步机型(参考"乔山治愈系跑步机"爆款)
        '【封面版式 — 治愈系情感型】\n'
        '- 顶部约 28% 区域叠加自上而下的深色渐变暗角(半透明黑,只压暗光线,不改色)\n'
        '- 顶部偏左:主标「{main_title}」超大粗黑体(类似阿里巴巴普惠体 Heavy),'
        '黑色字身 #1A1A1A + 白色厚描边 8px + 黑色 drop shadow 8px,占顶部 1/4 高\n'
        '- 主标下方一行小一号副标「{subtitle}」白色字 + 黑色细描边 2px,可自然两行换行\n'
        '- 底部约 25% 区域叠加自下而上的深色渐变暗角\n'
        '- 底部偏上:花字「{hua_text}」超大粗黑体,**双层立体描边花字**——'
        '浅蓝主体 #6FC4F5 + 白色厚描边 6px + 深蓝 #1B4196 偏移阴影向右下 10px(叠 3 层做立体厚度)\n'
        '- 右上角红底白字英文品牌矩形小徽章(若文案中无品牌名,省略)\n',

        # A2 · 红色立体描边·情感型(参考"有氧 KPI"爆款)
        '【封面版式 — 红蓝双色情感型】\n'
        '- 顶部局部暗角\n'
        '- 主标「{main_title}」位于画面上部偏左,超大粗黑体,**红色立体描边花字**——'
        '红色 #C72A2A 字身 + 白色厚描边 8px + 黑色阴影,字体微倾斜 3 度增加动感,占顶部 1/3\n'
        '- 副标「{subtitle}」白色细字 + 黑色细描边,位于主标右下方,可两行\n'
        '- 底部局部暗角\n'
        '- 底部偏上:花字「{hua_text}」**蓝色立体描边大字**——浅蓝 #6FC4F5 + 白色厚描边 6px + 深蓝偏移阴影\n'
        '- 花字下方一行极小白色细字写产品系列名(可选)\n',

        # A3 · 花字穿插·清爽型(参考"翘臀燃脂骑行法"爆款)
        '【封面版式 — 花字穿插清爽型】\n'
        '- 主标「{main_title}」位于画面**中上部**,**淡蓝色立体描边超大花字**——'
        '主体 #88C8EE + 白描边 6px + 深蓝偏移阴影(向右下叠 3 层立体感),字号占画面宽度 80%\n'
        '- 主标允许被原图中的人物/产品**前后景穿插遮挡**(局部被主体挡住部分笔画),营造层次\n'
        '- 左下角圆角矩形色块(mint 色 #B8E8D8),内放黑色字「@账号」或副标「{subtitle}」\n'
        '- 右下角圆角矩形色块(蓝色 #88B8E0),内放白色字「{hua_text}」\n'
        '- 整体画面顶部 5% 可叠加淡蓝白柔光(不破坏原图)\n',

        # A4 · 顶部巨大花字·氛围型(参考"跑步机瘦身指南"爆款)
        '【封面版式 — 顶部巨字氛围型】\n'
        '- 顶部 35% 区域:**超大蓝色立体描边花字**「{main_title}」横跨画面顶部,'
        '主体 #88C8EE + 白色描边 7px + 深蓝偏移阴影(立体感)\n'
        '- 花字的下半部分允许被原图主体(人物/产品)前后景穿插\n'
        '- 左下角圆角胶囊色块(蓝色 #88B8E0)内放白色细字副标「{subtitle}」\n'
        '- 右下角小一行白色细字「{hua_text}」描黑边\n'
        '- 右上角品牌小徽章(可选)\n',

        # A5 · 莫兰迪治愈型(衍生)
        '【封面版式 — 莫兰迪治愈极简型】\n'
        '- 主标「{main_title}」深棕色 #5C4A3C 粗体大字,位于画面顶部偏左,'
        '字形优雅、有细微烫金高光,占顶部 1/4\n'
        '- 副标「{subtitle}」奶咖色 #C9B0A0 细字签名感,位于主标下方,排版微下垂\n'
        '- 花字「{hua_text}」白底深棕边圆角胶囊贴纸,排版灵活,可放右下\n'
        '- 整体允许叠加极轻微的奶咖色调滤镜(不破坏原图主体)\n',
    ],

    '干货教程': [
        # B1 · 大白字英文型号·测评(参考"SONY A7M5 测评"爆款)
        '【封面版式 — 测评大白字英文型号型】\n'
        '- 画面右侧约 40% 区域:超大粗体英文产品名/型号「{main_title}」,'
        '纯白色 #FFFFFF 字 + 极薄黑色细阴影,**无描边**,占垂直 1/3 高,简洁高级\n'
        '- 主标正上方一行:黑底白字矩形品牌小徽章(若文案含品牌)\n'
        '- 画面底部:亮橙色 #FF7A1A 圆形大徽章(直径占画面 15%),内白色粗字「{hua_text}」,'
        '徽章右侧跟黑色中文细字「{subtitle}」\n'
        '- 整体可叠加轻微冷灰高级调滤镜(降饱和、轻冷),克制不堆砌\n'
        '- 不添加暗角,保留原图清晰度\n',

        # B2 · 撕纸胶带·杂志(参考"富士相机怎么选"爆款)
        '【封面版式 — 撕纸胶带杂志型】\n'
        '- 画面顶部 1/3 区域:**米色撕纸贴底**(纸张质感,边缘不规则毛边,微旋转 -2 度),'
        '其上叠加主标「{main_title}」超大粗黑体字 #1A1A1A,占撕纸贴大部分面积\n'
        '- 撕纸贴上方:浅绿色或米色**胶带胶贴感斜贴**,内放小标「{subtitle}」黑色字\n'
        '- 撕纸贴下方:深绿色 #5A6B3F 矩形色块,内放白色细字辅助副标或「{hua_text}」\n'
        '- 整体叠加米色复古胶片做旧调滤镜(不破坏原图主体)\n'
        '- 画面边角少量手绘点缀(波浪线、星星 ★、对勾 ✓ 小符号)\n',

        # B3 · 笔记本手写·学霸型
        '【封面版式 — 笔记本手写学霸型】\n'
        '- 主标「{main_title}」超大粗黑体,**关键名词用黄色 #FFD42D 不规则色块高亮覆盖**\n'
        '- 主标位于画面顶部 1/3 区域\n'
        '- 副标「{subtitle}」黑色细字,位于主标正下方\n'
        '- 蓝色 #1A6BCF **不规则手绘马克笔粗边框**环绕画面四周(像即兴涂的)\n'
        '- 顶部一行小字 # 话题标签\n'
        '- 花字「{hua_text}」白底黑描边圆角胶囊小贴纸,可多个排列\n',

        # B4 · 警示避雷·红黄型
        '【封面版式 — 避雷警示红黄型】\n'
        '- 主标「{main_title}」超大粗黑体 + **红色 #E53935 厚描边 6px** 或红色下划线,'
        '位于画面顶部 1/3\n'
        '- 主标旁边:红色感叹号 ⚠ 图标小装饰\n'
        '- 副标「{subtitle}」**红底 #E53935 白字横条**强调,占画面中部约 8% 高\n'
        '- 花字「{hua_text}」黄色 #FFD42D 圆角色块 + 黑色粗字,放在右下角标位置\n'
        '- 整体可叠加米黄色调滤镜(不破坏原图),警示感强\n',

        # B5 · Q&A 问答型
        '【封面版式 — Q&A 问答互动型】\n'
        '- 主标「{main_title}」黑色粗体大字,部分字加问号 / 感叹号增强情绪,'
        '位于画面顶部 1/3\n'
        '- 主标右侧或上方:**超大问号「?」装饰元素**(浅蓝或浅绿不规则色块形状)\n'
        '- 副标「{subtitle}」**白底黑边圆角胶囊**形状(像"答案标签")\n'
        '- 花字「{hua_text}」**白色对话气泡**形状(尾巴指向产品方向),内含小字,可多行\n'
        '- 顶部或底部少量浅蓝/浅绿色块拼接装饰\n',
    ],

    '促销爆款': [
        # C1 · 涂鸦跳跃·生活感(参考"现在冰箱都卷这些"爆款)
        '【封面版式 — 涂鸦跳跃生活感】\n'
        '- 顶部 1/4 区域:主标「{main_title}」**红色立体描边粉色填充大字**,'
        '字体**微倾斜 -5 度**,粉色 #FFB8C0 主体 + 红色 #C72A2A 描边 6px + 白色描边外圈 4px,'
        '极有跳跃感\n'
        '- 中部画面右侧:副标「{subtitle}」黑色花体小字斜贴,微旋转 -3 度\n'
        '- 底部 1/4 区域:花字「{hua_text}」**黄黑组合大字**——黄色 #FFD42D 字身 + 黑色描边 6px,'
        '微倾斜 5 度,排版活泼\n'
        '- 整体可叠加轻微暖黄色调滤镜(不破坏原图)\n',

        # C2 · 大促爆款·红黄拼贴
        '【封面版式 — 双 11 大促主视觉型】\n'
        '- 主标「{main_title}」**黄字 #FFD42D + 红色厚描边 6px + 黑色斜阴影**,'
        '占画面顶部 35%,极强视觉冲击\n'
        '- 副标「{subtitle}」白底红字圆角胶囊,右上贴一个红色"爆"字小角标\n'
        '- 画面顶部和底部边缘:浅色半透明 "NEW" / "SALE" 英文衬底大字(降透明度作底)\n'
        '- 花字「{hua_text}」红黄相间促销贴纸,微旋转,可多行堆叠\n'
        '- 整体红黄高饱和调子(只在文字/色块,不染产品)\n',

        # C3 · 限时奢华·金色型
        '【封面版式 — 奢侈品限时金色型】\n'
        '- 主标「{main_title}」**金色 #D4AF37 粗黑大字 + 黑色阴影 + 细金色描边**,'
        '占画面上部 1/3\n'
        '- 主标上方一行:白底红字 #C72A2A 小标签写"限时"\n'
        '- 主标周围装饰:皇冠 / 奖杯小图标点缀\n'
        '- 副标「{subtitle}」白色细字,位于主标下方\n'
        '- 花字「{hua_text}」亮金色圆角胶囊,可多行堆叠,放右下\n'
        '- 画面四周边缘可叠加深红/暗紫渐变滤镜(只压暗边缘,不遮挡产品)\n',

        # C4 · 双 11 节点·黑红动感
        '【封面版式 — 黑红动感双 11 型】\n'
        '- 主标「{main_title}」**立体描边白字 + 红色阴影**,'
        '其中关键词单独用**黄色 #FFD42D 高亮覆盖**,占画面顶部 1/3\n'
        '- 副标「{subtitle}」白色细字,排版可微倾斜增加动感\n'
        '- 画面四角:爆炸星形红色装饰,加 "NEW" / "HOT" / "SALE" 英文衬底大字(降透明度)\n'
        '- 花字「{hua_text}」**红色价格爆炸贴纸**,可多行,有透视感\n'
        '- 整体保留产品照原色,只在文字/装饰处用黑红配色\n',

        # C5 · 价格爆炸折扣型
        '【封面版式 — 价格爆炸折扣型】\n'
        '- 主标「{main_title}」**红黑双色立体描边大字**——红色 #C72A2A 字身 + 黑色描边 6px + 白色外描边 4px,'
        '关键数字单独加大字号突出\n'
        '- 副标「{subtitle}」黑底白字矩形横条强调\n'
        '- 画面右上或左下:"省 XX 元" 或 "-50%" **黄底红字星形爆炸贴纸**,直径占画面 20%\n'
        '- 花字「{hua_text}」红黄相间贴纸,多行堆叠,极强转化导向\n'
        '- 整体保留产品照原色,促销元素在四周,不遮挡产品主体\n',
    ],
}

DEFAULT_STYLE = '种草氛围'


# ═════════════ 文案类型 → 封面风格池 自动映射 ═════════════
# 业务员选了哪类文案,封面就自动落到对应的风格池里抽变体出图
# 业务员不需要再二次选择"封面风格"

COPY_TYPE_TO_STYLE = {
    '种草': '种草氛围',
    '场景': '种草氛围',
    '干货': '干货教程',
    '促销': '促销爆款',
}


def map_copy_type_to_style(copy_type: str) -> str:
    """文案类型 → 封面风格名;落到对应风格池"""
    return COPY_TYPE_TO_STYLE.get((copy_type or '').strip(), DEFAULT_STYLE)


# ═════════════ 通用渲染要求(末尾拼接)═════════════

TEXT_REQUIREMENTS = (
    '\n【文字渲染要求 — 严格执行】\n'
    '1. 所有中文字必须**清晰可读、字形完整、一字不差**,严格按提供内容渲染,'
    '不得增、删、改、自创字\n'
    '2. 字号层级:主标 > 花字 > 副标,差异要明显\n'
    '3. 字体:粗体中文 sans-serif(类似阿里巴巴普惠体 Heavy 或思源黑体 Heavy)\n'
    '4. 所有描边都饱满清晰,不糊不晕\n'
    '5. 文字图层与产品/人物的层级关系清晰,文字不遮挡产品的关键 logo 和细节\n'
    '6. 若文案中提供的某个字段为空(显示为"(无副标题)"或"(无花字)"),'
    '则该元素**不要渲染**\n'
)

QUALITY_BOOST = (
    '\n【整体品质收尾】\n'
    '- 小红书爆款封面级精度,trending Xiaohongshu cover, professional layout design\n'
    '- 文字排版精致,像被设计师在 Figma/PS 里精心调过版\n'
    '- 整张图保留产品照原貌,封面元素是叠加图层而非重绘\n'
    '- 不要添加水印、不要 AI 签名、不要随机英文字符\n'
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

            # 优先用 copy_type 自动映射风格(新版),否则用旧的 style 参数(向后兼容)
            copy_type = (req.get('copy_type') or '').strip()
            if copy_type:
                style = map_copy_type_to_style(copy_type)
            else:
                style = (req.get('style') or '').strip() or DEFAULT_STYLE

            user = req.get('_user') or {}
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
