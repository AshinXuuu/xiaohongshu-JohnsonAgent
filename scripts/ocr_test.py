"""豆包视觉 OCR 概念验证脚本。

用法:
    python3 scripts/ocr_test.py <PDF 路径> [起始页] [页数]

例如:
    python3 scripts/ocr_test.py 产品库/乔山Johnson/智能跑步机TX3/JS26_跑步机TX3说明书.pdf 1 3

会:
  1. 读 .env 拿 DOUBAO_API_KEY
  2. 用 PyMuPDF 把指定页渲染成 PNG(300dpi)
  3. 每页调一次豆包视觉 API,返回 OCR 文本
  4. 把每页结果打印出来,顺便存到 out/ocr_<pdf名>_p<页号>.txt 便于检视
"""
import os
import sys
import ssl
import json
import base64
import time
import urllib.request
import urllib.error
from pathlib import Path

import fitz  # PyMuPDF


def _build_ssl_context():
    """优先用 certifi 的根证书,解决 Mac Python 默认证书空缺。
    没装 certifi 就用系统默认。"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CTX = _build_ssl_context()


# ── 配置 ─────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
ARK_URL = 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'
# 豆包视觉模型(火山引擎 ARK):用 1.5-vision-pro,中文 OCR 最强档
# 模型名取 ARK 控制台「模型推理」里的 endpoint id 或 model id
DOUBAO_VISION_MODEL = os.environ.get(
    'DOUBAO_VISION_MODEL',
    'doubao-1-5-vision-pro-32k-250115',
)
RENDER_DPI = 200  # 200 足够清晰且压缩后 < 4MB
HTTP_TIMEOUT = 90

OCR_PROMPT = (
    "你是专业的 OCR 文字识别引擎。请逐字识别下面这张图(产品说明书的某一页)里的"
    "所有可见文字,严格按照原始版面顺序输出(从上到下、从左到右,分栏的图按栏顺序)。\n"
    "要求:\n"
    "1. 只输出识别到的文字本身,不要添加任何解释、概括、润色。\n"
    "2. 数字、单位(cm/kg/W/dB/CHP 等)、型号、英文,严格按原图照抄,不要纠正、不要补全。\n"
    "3. 段落之间空一行;同一段内换行用单 \\n。\n"
    "4. 如果图上有表格,用 Markdown 表格语法输出。\n"
    "5. 图上的纯图标/插画/示意图,不需要描述,直接跳过。\n"
    "6. 如果整页根本没有文字(例如纯封面图、纯产品照),只输出 '[无文字内容]'。"
)


def load_env():
    """简单 .env 加载;鲁棒处理引号、export 前缀、Windows 换行"""
    env_file = ROOT / '.env'
    if not env_file.exists():
        print(f'⚠️  .env 文件不存在: {env_file}')
        return
    for raw in env_file.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip().lstrip('﻿')  # 去 BOM
        if line.startswith('export '):
            line = line[7:].strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        k = k.strip()
        v = v.strip()
        # 去掉首尾配对的引号
        if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
            v = v[1:-1]
        os.environ.setdefault(k, v)


def pdf_page_to_png_b64(pdf_path: Path, page_idx: int, dpi: int = RENDER_DPI) -> bytes:
    """把 PDF 第 page_idx 页(0-based)渲染成 PNG 字节"""
    doc = fitz.open(pdf_path)
    page = doc[page_idx]
    pix = page.get_pixmap(dpi=dpi)
    png_bytes = pix.tobytes('png')
    doc.close()
    return png_bytes


def call_doubao_vision(image_b64: str, api_key: str) -> tuple[str | None, str | None]:
    """单页 OCR,返回 (text, error)"""
    payload = {
        'model': DOUBAO_VISION_MODEL,
        'messages': [
            {
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': OCR_PROMPT},
                    {
                        'type': 'image_url',
                        'image_url': {
                            'url': f'data:image/png;base64,{image_b64}',
                        },
                    },
                ],
            },
        ],
        'temperature': 0.0,  # OCR 必须确定性,不要"创意"
        'max_tokens': 4000,
    }
    req = urllib.request.Request(
        ARK_URL,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=SSL_CTX) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        content = data['choices'][0]['message']['content']
        return content, None
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='ignore') if e.fp else ''
        return None, f'HTTP {e.code}: {detail[:500]}'
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    load_env()
    api_key = os.environ.get('DOUBAO_API_KEY', '').strip()
    if not api_key:
        print('❌ DOUBAO_API_KEY 未配置')
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    if not pdf_path.is_absolute():
        pdf_path = ROOT / pdf_path
    if not pdf_path.exists():
        print(f'❌ PDF 不存在: {pdf_path}')
        sys.exit(1)

    start_page = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    num_pages = int(sys.argv[3]) if len(sys.argv) > 3 else 3

    doc = fitz.open(pdf_path)
    total = len(doc)
    doc.close()

    out_dir = ROOT / 'out' / 'ocr_test'
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f'文件: {pdf_path.name}({total} 页)')
    print(f'模型: {DOUBAO_VISION_MODEL}')
    print(f'识别范围: 第 {start_page} 页起,共 {num_pages} 页')
    print('═' * 60)

    for i in range(num_pages):
        page_no = start_page + i  # 1-based
        if page_no > total:
            break
        page_idx = page_no - 1
        t0 = time.time()
        png_bytes = pdf_page_to_png_b64(pdf_path, page_idx)
        img_size_kb = len(png_bytes) // 1024
        image_b64 = base64.b64encode(png_bytes).decode('ascii')
        text, err = call_doubao_vision(image_b64, api_key)
        elapsed = time.time() - t0

        print(f'\n━━━ 第 {page_no} 页(图 {img_size_kb} KB,耗时 {elapsed:.1f}s)━━━')
        if err:
            print(f'❌ {err}')
            continue
        print(text)

        # 落盘
        safe_name = pdf_path.stem.replace(' ', '_').replace('/', '_')
        out_file = out_dir / f'{safe_name}_p{page_no:02d}.txt'
        out_file.write_text(text, encoding='utf-8')

    print('\n' + '═' * 60)
    print(f'输出文件目录: {out_dir}')


if __name__ == '__main__':
    main()
