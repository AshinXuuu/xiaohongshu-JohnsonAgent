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
    '  ✗ **不要渲染任何品牌 logo、品牌英文名、产品型号字、二维码、watermark 水印**——\n'
    '    这些 AI 写出来 100% 会错字,品牌名/型号一律不要出现在画面里,\n'
    '    业务员后期会自己在 PS/Canva 里手动加 logo\n'
    '\n'
)


# ═════════════ 风格模板池(30 个变体 = 15 套版式 × 2 套配色)═════════════
# 命名规则:字母+数字 = 版式(A1/A2/.../C5),后缀 b = 配色变体
# 每次出图从对应风格池(10 套)随机抽 3 套不同模板并发出图

STYLE_PROMPT_POOLS = {
    '种草氛围': [
        # ───────────── A1 治愈系情感型(2 配色)─────────────
        # A1a · 蓝色冷调(参考"乔山治愈系跑步机"爆款)
        '【封面版式 — 治愈系情感型 · 冷蓝调】\n'
        '- 顶部约 28% 区域叠加自上而下的深色渐变暗角(半透明黑,只压暗光线,不改色)\n'
        '- 顶部偏左:主标「{main_title}」超大粗黑体(类似阿里巴巴普惠体 Heavy),'
        '黑色字身 #1A1A1A + 白色厚描边 8px + 黑色 drop shadow 8px,占顶部 1/4 高\n'
        '- 主标下方一行小一号副标「{subtitle}」白色字 + 黑色细描边 2px,可自然两行换行\n'
        '- 底部约 25% 区域叠加自下而上的深色渐变暗角\n'
        '- 底部偏上:花字「{hua_text}」超大粗黑体,**双层立体描边花字**——'
        '浅蓝主体 #6FC4F5 + 白色厚描边 6px + 深蓝 #1B4196 偏移阴影向右下 10px(叠 3 层做立体厚度)\n',

        # A1b · 暖橙调(同版式,换暖系配色,适合晨光/秋意/温暖主题)
        '【封面版式 — 治愈系情感型 · 暖橙调】\n'
        '- 顶部约 28% 区域叠加自上而下的深色渐变暗角(半透明黑,只压暗光线,不改色)\n'
        '- 顶部偏左:主标「{main_title}」超大粗黑体,'
        '深棕字身 #3D2914 + 米白厚描边 8px #FBF1E0 + 深棕 drop shadow 8px,占顶部 1/4 高\n'
        '- 主标下方一行小一号副标「{subtitle}」米白色字 + 深棕细描边 2px,可两行换行\n'
        '- 底部约 25% 区域叠加自下而上的深色渐变暗角\n'
        '- 底部偏上:花字「{hua_text}」超大粗黑体,**双层立体描边花字**——'
        '暖橙主体 #FFB444 + 米白厚描边 6px + 深棕 #6B3E12 偏移阴影向右下 10px(叠 3 层做立体厚度)\n',

        # ───────────── A2 红蓝双色情感型(2 配色)─────────────
        # A2a · 红蓝经典(参考"有氧 KPI"爆款)
        '【封面版式 — 红蓝双色情感型 · 红蓝经典】\n'
        '- 顶部局部暗角\n'
        '- 主标「{main_title}」位于画面上部偏左,超大粗黑体,**红色立体描边花字**——'
        '红色 #C72A2A 字身 + 白色厚描边 8px + 黑色阴影,字体微倾斜 3 度增加动感,占顶部 1/3\n'
        '- 副标「{subtitle}」白色细字 + 黑色细描边,位于主标右下方,可两行\n'
        '- 底部局部暗角\n'
        '- 底部偏上:花字「{hua_text}」**蓝色立体描边大字**——浅蓝 #6FC4F5 + 白色厚描边 6px + 深蓝偏移阴影\n',

        # A2b · 紫粉双色(同版式,女性向更强,适合女性产品/情感共鸣)
        '【封面版式 — 红蓝双色情感型 · 紫粉变体】\n'
        '- 顶部局部暗角\n'
        '- 主标「{main_title}」位于画面上部偏左,超大粗黑体,**紫色立体描边花字**——'
        '紫色 #7B2D8E 字身 + 白色厚描边 8px + 深紫 #3D0F4A 阴影,字体微倾斜 3 度,占顶部 1/3\n'
        '- 副标「{subtitle}」白色细字 + 深紫细描边,位于主标右下方,可两行\n'
        '- 底部局部暗角\n'
        '- 底部偏上:花字「{hua_text}」**粉色立体描边大字**——粉色 #FF7BAC + 白色厚描边 6px + 深粉 #B8266A 偏移阴影\n',

        # ───────────── A3 花字穿插清爽型(2 配色)─────────────
        # A3a · 淡蓝 mint(参考"翘臀燃脂骑行法"爆款)
        '【封面版式 — 花字穿插清爽型 · 淡蓝 mint】\n'
        '- 主标「{main_title}」位于画面**中上部**,**淡蓝色立体描边超大花字**——'
        '主体 #88C8EE + 白描边 6px + 深蓝偏移阴影(向右下叠 3 层立体感),字号占画面宽度 80%\n'
        '- 主标允许被原图中的人物/产品**前后景穿插遮挡**(局部被主体挡住部分笔画),营造层次\n'
        '- 左下角圆角矩形色块(mint 色 #B8E8D8),内放黑色字「@账号」或副标「{subtitle}」\n'
        '- 右下角圆角矩形色块(蓝色 #88B8E0),内放白色字「{hua_text}」\n'
        '- 整体画面顶部 5% 可叠加淡蓝白柔光(不破坏原图)\n',

        # A3b · 粉绿活力(同版式,换粉绿活泼调,适合活力/年轻向)
        '【封面版式 — 花字穿插清爽型 · 粉绿活力】\n'
        '- 主标「{main_title}」位于画面**中上部**,**浅粉立体描边超大花字**——'
        '主体 #FFB8D4 + 白描边 6px + 深粉 #C9437A 偏移阴影(向右下叠 3 层立体感),字号占画面宽度 80%\n'
        '- 主标允许被原图中的人物/产品**前后景穿插遮挡**,营造层次\n'
        '- 左下角圆角矩形色块(薄荷绿 #B0E8C5),内放黑色字「@账号」或副标「{subtitle}」\n'
        '- 右下角圆角矩形色块(鹅黄 #FFE382),内放深棕字「{hua_text}」\n'
        '- 整体画面顶部 5% 可叠加淡粉柔光(不破坏原图)\n',

        # ───────────── A4 顶部巨字氛围型(2 配色)─────────────
        # A4a · 蓝色氛围(参考"跑步机瘦身指南"爆款)
        '【封面版式 — 顶部巨字氛围型 · 蓝色氛围】\n'
        '- 顶部 35% 区域:**超大蓝色立体描边花字**「{main_title}」横跨画面顶部,'
        '主体 #88C8EE + 白色描边 7px + 深蓝偏移阴影(立体感)\n'
        '- 花字的下半部分允许被原图主体(人物/产品)前后景穿插\n'
        '- 左下角圆角胶囊色块(蓝色 #88B8E0)内放白色细字副标「{subtitle}」\n'
        '- 右下角小一行白色细字「{hua_text}」描黑边\n',

        # A4b · 燃情红黑(同版式,换强烈红黑调,适合燃脂/运动激情主题)
        '【封面版式 — 顶部巨字氛围型 · 燃情红黑】\n'
        '- 顶部 35% 区域:**超大红色立体描边花字**「{main_title}」横跨画面顶部,'
        '主体 #E63946 + 白色描边 7px + 黑色 #0F0F0F 偏移阴影(立体感)\n'
        '- 花字的下半部分允许被原图主体(人物/产品)前后景穿插\n'
        '- 左下角圆角胶囊色块(深灰 #2A2A2A)内放亮黄 #FFD42D 细字副标「{subtitle}」\n'
        '- 右下角小一行白色细字「{hua_text}」描黑边\n',

        # ───────────── A5 莫兰迪治愈极简(2 配色)─────────────
        # A5a · 奶咖深棕(衍生)
        '【封面版式 — 莫兰迪治愈极简型 · 奶咖调】\n'
        '- 主标「{main_title}」深棕色 #5C4A3C 粗体大字,位于画面顶部偏左,'
        '字形优雅、有细微烫金高光,占顶部 1/4\n'
        '- 副标「{subtitle}」奶咖色 #C9B0A0 细字签名感,位于主标下方,排版微下垂\n'
        '- 花字「{hua_text}」白底深棕边圆角胶囊贴纸,排版灵活,可放右下\n'
        '- 整体允许叠加极轻微的奶咖色调滤镜(不破坏原图主体)\n',

        # A5b · 北欧蓝灰(同版式,换冷调蓝灰,适合极简家居/北欧风)
        '【封面版式 — 莫兰迪治愈极简型 · 北欧蓝灰】\n'
        '- 主标「{main_title}」深蓝灰色 #3D5365 粗体大字,位于画面顶部偏左,'
        '字形优雅、有细微银色高光,占顶部 1/4\n'
        '- 副标「{subtitle}」雾蓝色 #88A0B5 细字签名感,位于主标下方,排版微下垂\n'
        '- 花字「{hua_text}」浅灰白 #D8DEE4 底 + 深蓝灰边圆角胶囊贴纸,排版灵活,可放右下\n'
        '- 整体允许叠加极轻微的冷灰调滤镜(不破坏原图主体)\n',
    ],

    '干货教程': [
        # ───────────── B1 测评大字主标型(2 配色)─────────────
        # B1a · 白主标 + 橙徽章(参考"SONY A7M5 测评"爆款)
        '【封面版式 — 测评大字主标型 · 白橙调】\n'
        '- 画面右侧约 40% 区域:超大粗黑体中文主标「{main_title}」,'
        '纯白色 #FFFFFF 字 + 极薄黑色细阴影,**无描边**,占垂直 1/3 高,简洁高级\n'
        '- 画面底部:亮橙色 #FF7A1A 圆形大徽章(直径占画面 15%),内白色粗字「{hua_text}」,'
        '徽章右侧跟黑色中文细字「{subtitle}」\n'
        '- 整体可叠加轻微冷灰高级调滤镜(降饱和、轻冷),克制不堆砌\n'
        '- 不添加暗角,保留原图清晰度\n',

        # B1b · 黑主标 + 金徽章(同版式,商务高级感)
        '【封面版式 — 测评大字主标型 · 黑金高级】\n'
        '- 画面右侧约 40% 区域:超大粗黑体中文主标「{main_title}」,'
        '纯黑色 #0F0F0F 字 + 细金色 #D4AF37 细描边 1px,占垂直 1/3 高,沉稳商务\n'
        '- 画面底部:金色 #D4AF37 圆形大徽章(直径占画面 15%),内深黑粗字「{hua_text}」,'
        '徽章右侧跟黑色中文细字「{subtitle}」\n'
        '- 整体可叠加轻微暖米色高级调滤镜(微暖、微提亮),克制不堆砌\n'
        '- 不添加暗角,保留原图清晰度\n',

        # ───────────── B2 撕纸胶带杂志型(2 配色)─────────────
        # B2a · 米色撕纸 + 深绿(参考"富士相机怎么选"爆款)
        '【封面版式 — 撕纸胶带杂志型 · 米色复古】\n'
        '- 画面顶部 1/3 区域:**米色撕纸贴底**(纸张质感,边缘不规则毛边,微旋转 -2 度),'
        '其上叠加主标「{main_title}」超大粗黑体字 #1A1A1A,占撕纸贴大部分面积\n'
        '- 撕纸贴上方:浅绿色或米色**胶带胶贴感斜贴**,内放小标「{subtitle}」黑色字\n'
        '- 撕纸贴下方:深绿色 #5A6B3F 矩形色块,内放白色细字辅助副标或「{hua_text}」\n'
        '- 整体叠加米色复古胶片做旧调滤镜(不破坏原图主体)\n'
        '- 画面边角少量手绘点缀(波浪线、星星 ★、对勾 ✓ 小符号)\n',

        # B2b · 牛皮纸 + 复古红(同版式,换牛皮卡其底色更复古)
        '【封面版式 — 撕纸胶带杂志型 · 牛皮纸复古红】\n'
        '- 画面顶部 1/3 区域:**牛皮纸卡其色 #C9A878 撕纸贴底**(纸张质感,边缘不规则毛边,微旋转 -2 度),'
        '其上叠加主标「{main_title}」超大粗黑体字 #2B1810,占撕纸贴大部分面积\n'
        '- 撕纸贴上方:复古红 #A6342B **胶带胶贴感斜贴**,内放小标「{subtitle}」白色字\n'
        '- 撕纸贴下方:复古红 #A6342B 矩形色块,内放米白细字辅助副标或「{hua_text}」\n'
        '- 整体叠加暖棕色做旧滤镜(不破坏原图主体)\n'
        '- 画面边角少量手绘点缀(波浪线、星星 ★、对勾 ✓ 小符号)\n',

        # ───────────── B3 笔记本手写学霸型(2 配色)─────────────
        # B3a · 蓝边框 + 黄高亮
        '【封面版式 — 笔记本手写学霸型 · 蓝黄高亮】\n'
        '- 主标「{main_title}」超大粗黑体,**关键名词用黄色 #FFD42D 不规则色块高亮覆盖**\n'
        '- 主标位于画面顶部 1/3 区域\n'
        '- 副标「{subtitle}」黑色细字,位于主标正下方\n'
        '- 蓝色 #1A6BCF **不规则手绘马克笔粗边框**环绕画面四周(像即兴涂的)\n'
        '- 顶部一行小字 # 话题标签\n'
        '- 花字「{hua_text}」白底黑描边圆角胶囊小贴纸,可多个排列\n',

        # B3b · 绿边框 + 荧光粉高亮(同版式,换清新绿+荧光粉,适合女性受众)
        '【封面版式 — 笔记本手写学霸型 · 绿粉荧光】\n'
        '- 主标「{main_title}」超大粗黑体,**关键名词用荧光粉 #FF85C0 不规则色块高亮覆盖**\n'
        '- 主标位于画面顶部 1/3 区域\n'
        '- 副标「{subtitle}」深绿色 #2E8B57 细字,位于主标正下方\n'
        '- 草绿色 #2E8B57 **不规则手绘马克笔粗边框**环绕画面四周(像即兴涂的)\n'
        '- 顶部一行小字 # 话题标签\n'
        '- 花字「{hua_text}」浅粉 #FFE4F0 底 + 深粉描边圆角胶囊小贴纸,可多个排列\n',

        # ───────────── B4 避雷警示红黄型(2 配色)─────────────
        # B4a · 红黄经典警示
        '【封面版式 — 避雷警示红黄型 · 经典红黄】\n'
        '- 主标「{main_title}」超大粗黑体 + **红色 #E53935 厚描边 6px** 或红色下划线,'
        '位于画面顶部 1/3\n'
        '- 主标旁边:红色感叹号 ⚠ 图标小装饰\n'
        '- 副标「{subtitle}」**红底 #E53935 白字横条**强调,占画面中部约 8% 高\n'
        '- 花字「{hua_text}」黄色 #FFD42D 圆角色块 + 黑色粗字,放在右下角标位置\n'
        '- 整体可叠加米黄色调滤镜(不破坏原图),警示感强\n',

        # B4b · 深蓝橙警示(同版式,蓝橙互补色,警示感更冷静专业)
        '【封面版式 — 避雷警示红黄型 · 深蓝橙警示】\n'
        '- 主标「{main_title}」超大粗黑体 + **深蓝 #003366 厚描边 6px** 或深蓝下划线,'
        '位于画面顶部 1/3\n'
        '- 主标旁边:深蓝色感叹号 ⚠ 图标小装饰\n'
        '- 副标「{subtitle}」**海蓝底 #1E40AF 白字横条**强调,占画面中部约 8% 高\n'
        '- 花字「{hua_text}」橙色 #F59E0B 圆角色块 + 深蓝粗字,放在右下角标位置\n'
        '- 整体可叠加冷蓝调滤镜(不破坏原图),专业严肃感\n',

        # ───────────── B5 Q&A 问答型(2 配色)─────────────
        # B5a · 浅蓝/浅绿色块
        '【封面版式 — Q&A 问答互动型 · 蓝绿清新】\n'
        '- 主标「{main_title}」黑色粗体大字,部分字加问号 / 感叹号增强情绪,'
        '位于画面顶部 1/3\n'
        '- 主标右侧或上方:**超大问号「?」装饰元素**(浅蓝或浅绿不规则色块形状)\n'
        '- 副标「{subtitle}」**白底黑边圆角胶囊**形状(像"答案标签")\n'
        '- 花字「{hua_text}」**白色对话气泡**形状(尾巴指向产品方向),内含小字,可多行\n'
        '- 顶部或底部少量浅蓝/浅绿色块拼接装饰\n',

        # B5b · 复古橙紫(同版式,换复古橙紫,更具文艺/复古感)
        '【封面版式 — Q&A 问答互动型 · 复古橙紫】\n'
        '- 主标「{main_title}」深紫色 #4A2C5A 粗体大字,部分字加问号 / 感叹号增强情绪,'
        '位于画面顶部 1/3\n'
        '- 主标右侧或上方:**超大问号「?」装饰元素**(复古橙 #E07A5F 不规则色块形状)\n'
        '- 副标「{subtitle}」**深紫底 #4A2C5A 米白字圆角胶囊**形状(像"答案标签")\n'
        '- 花字「{hua_text}」**紫粉色 #C9B6DD 对话气泡**形状(尾巴指向产品方向),内深紫小字,可多行\n'
        '- 顶部或底部少量复古橙 / 紫粉色块拼接装饰\n',
    ],

    '促销爆款': [
        # ───────────── C1 涂鸦跳跃生活感(2 配色)─────────────
        # C1a · 粉红黄黑跳跃(参考"现在冰箱都卷这些"爆款)
        '【封面版式 — 涂鸦跳跃生活感 · 粉黄经典】\n'
        '- 顶部 1/4 区域:主标「{main_title}」**红色立体描边粉色填充大字**,'
        '字体**微倾斜 -5 度**,粉色 #FFB8C0 主体 + 红色 #C72A2A 描边 6px + 白色描边外圈 4px,'
        '极有跳跃感\n'
        '- 中部画面右侧:副标「{subtitle}」黑色花体小字斜贴,微旋转 -3 度\n'
        '- 底部 1/4 区域:花字「{hua_text}」**黄黑组合大字**——黄色 #FFD42D 字身 + 黑色描边 6px,'
        '微倾斜 5 度,排版活泼\n'
        '- 整体可叠加轻微暖黄色调滤镜(不破坏原图)\n',

        # C1b · 蓝青糖果(同版式,换天蓝青绿,更年轻清爽)
        '【封面版式 — 涂鸦跳跃生活感 · 蓝青糖果】\n'
        '- 顶部 1/4 区域:主标「{main_title}」**深蓝立体描边天蓝填充大字**,'
        '字体**微倾斜 -5 度**,天蓝 #4FC3F7 主体 + 深蓝 #0D47A1 描边 6px + 白色描边外圈 4px,'
        '极有跳跃感\n'
        '- 中部画面右侧:副标「{subtitle}」深蓝色花体小字斜贴,微旋转 -3 度\n'
        '- 底部 1/4 区域:花字「{hua_text}」**青绿白字组合大字**——青绿 #00BFA5 字身 + 白色描边 6px,'
        '微倾斜 5 度,排版活泼\n'
        '- 整体可叠加轻微冷蓝色调滤镜(不破坏原图)\n',

        # ───────────── C2 双 11 大促主视觉型(2 配色)─────────────
        # C2a · 黄字红描边经典
        '【封面版式 — 双 11 大促主视觉型 · 黄红经典】\n'
        '- 主标「{main_title}」**黄字 #FFD42D + 红色厚描边 6px + 黑色斜阴影**,'
        '占画面顶部 35%,极强视觉冲击\n'
        '- 副标「{subtitle}」白底红字圆角胶囊,右上贴一个红色"爆"字小角标\n'
        '- 画面四角加红色爆炸星形装饰(纯几何形状,不要英文字)\n'
        '- 花字「{hua_text}」红黄相间促销贴纸,微旋转,可多行堆叠\n'
        '- 整体红黄高饱和调子(只在文字/色块,不染产品)\n',

        # C2b · 粉橙活力(同版式,粉橙年轻向,适合美妆/女性产品)
        '【封面版式 — 双 11 大促主视觉型 · 粉橙活力】\n'
        '- 主标「{main_title}」**橙色 #FF6F3C 字 + 粉红厚描边 6px #FFB8D4 + 黑色斜阴影**,'
        '占画面顶部 35%,极强视觉冲击\n'
        '- 副标「{subtitle}」白底紫红字圆角胶囊,右上贴一个紫红色 #C2185B"爆"字小角标\n'
        '- 画面四角加紫红爆炸星形装饰(纯几何形状,不要英文字)\n'
        '- 花字「{hua_text}」粉橙相间促销贴纸,微旋转,可多行堆叠\n'
        '- 整体粉橙高饱和调子(只在文字/色块,不染产品)\n',

        # ───────────── C3 奢侈品限时金色型(2 配色)─────────────
        # C3a · 金色 + 暗紫边
        '【封面版式 — 奢侈品限时金色型 · 金紫调】\n'
        '- 主标「{main_title}」**金色 #D4AF37 粗黑大字 + 黑色阴影 + 细金色描边**,'
        '占画面上部 1/3\n'
        '- 主标上方一行:白底红字 #C72A2A 小标签写"限时"\n'
        '- 主标周围装饰:皇冠 / 奖杯小图标点缀\n'
        '- 副标「{subtitle}」白色细字,位于主标下方\n'
        '- 花字「{hua_text}」亮金色圆角胶囊,可多行堆叠,放右下\n'
        '- 画面四周边缘可叠加深红/暗紫渐变滤镜(只压暗边缘,不遮挡产品)\n',

        # C3b · 银钻冷质感(同版式,换银+墨蓝,更高科技/极简奢侈)
        '【封面版式 — 奢侈品限时金色型 · 银钻冷调】\n'
        '- 主标「{main_title}」**银色 #B0B7C0 粗黑大字 + 黑色阴影 + 细银色描边**,'
        '占画面上部 1/3\n'
        '- 主标上方一行:白底墨蓝 #0F2D52 小标签写"限时"\n'
        '- 主标周围装饰:钻石 / 星辰小图标点缀\n'
        '- 副标「{subtitle}」浅银灰细字,位于主标下方\n'
        '- 花字「{hua_text}」银灰色圆角胶囊,可多行堆叠,放右下\n'
        '- 画面四周边缘可叠加墨蓝渐变滤镜(只压暗边缘,不遮挡产品)\n',

        # ───────────── C4 黑红动感双 11 型(2 配色)─────────────
        # C4a · 白字红影黄高亮
        '【封面版式 — 黑红动感双 11 型 · 白红黄】\n'
        '- 主标「{main_title}」**立体描边白字 + 红色阴影**,'
        '其中关键词单独用**黄色 #FFD42D 高亮覆盖**,占画面顶部 1/3\n'
        '- 副标「{subtitle}」白色细字,排版可微倾斜增加动感\n'
        '- 画面四角:红色爆炸星形装饰(纯几何形状,不要任何英文字)\n'
        '- 花字「{hua_text}」**红色价格爆炸贴纸**,可多行,有透视感\n'
        '- 整体保留产品照原色,只在文字/装饰处用黑红配色\n',

        # C4b · 电子紫绿(同版式,荧光紫+荧光绿,适合数码/潮玩/年轻向)
        '【封面版式 — 黑红动感双 11 型 · 电子紫绿】\n'
        '- 主标「{main_title}」**立体描边白字 + 荧光紫 #B928E0 阴影**,'
        '其中关键词单独用**荧光绿 #B6FF00 高亮覆盖**,占画面顶部 1/3\n'
        '- 副标「{subtitle}」白色细字,排版可微倾斜增加动感\n'
        '- 画面四角:荧光紫爆炸星形装饰(纯几何形状,不要任何英文字)\n'
        '- 花字「{hua_text}」**荧光紫价格爆炸贴纸**,可多行,有透视感\n'
        '- 整体保留产品照原色,只在文字/装饰处用紫绿电子配色\n',

        # ───────────── C5 价格爆炸折扣型(2 配色)─────────────
        # C5a · 红黑黄红经典
        '【封面版式 — 价格爆炸折扣型 · 红黑经典】\n'
        '- 主标「{main_title}」**红黑双色立体描边大字**——红色 #C72A2A 字身 + 黑色描边 6px + 白色外描边 4px,'
        '关键数字单独加大字号突出\n'
        '- 副标「{subtitle}」黑底白字矩形横条强调\n'
        '- 画面右上或左下:"省 XX 元" 或 "-50%" **黄底红字星形爆炸贴纸**,直径占画面 20%\n'
        '- 花字「{hua_text}」红黄相间贴纸,多行堆叠,极强转化导向\n'
        '- 整体保留产品照原色,促销元素在四周,不遮挡产品主体\n',

        # C5b · 蓝橙促销(同版式,蓝橙互补色,科技/家电感)
        '【封面版式 — 价格爆炸折扣型 · 蓝橙促销】\n'
        '- 主标「{main_title}」**蓝黑双色立体描边大字**——海蓝 #1565C0 字身 + 黑色描边 6px + 白色外描边 4px,'
        '关键数字单独加大字号突出\n'
        '- 副标「{subtitle}」深蓝底白字矩形横条强调\n'
        '- 画面右上或左下:"省 XX 元" 或 "-50%" **橙底白字星形爆炸贴纸 #F57C00**,直径占画面 20%\n'
        '- 花字「{hua_text}」蓝橙相间贴纸,多行堆叠,极强转化导向\n'
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
