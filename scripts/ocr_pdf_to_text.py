"""把一份 PDF 全量 OCR 成一个 txt 文件,便于人工通读 / 校对。

用法:
    python3 scripts/ocr_pdf_to_text.py <PDF 路径>
    python3 scripts/ocr_pdf_to_text.py <PDF 路径> 1 50         # 只跑 1-50 页
    python3 scripts/ocr_pdf_to_text.py <PDF 路径> 1 50 8       # 8 路并发

特点:
  - 4 路并发(可调),保证每页页号顺序在输出文件里正确
  - 实时进度 + 累计成本
  - 输出:out/<pdf_stem>_OCR.txt(带 ━━ 第 N 页 ━━ 分隔)
"""
import os
import re
import sys
import ssl
import json
import time
import base64
import urllib.request
import urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import fitz  # PyMuPDF


ROOT = Path(__file__).resolve().parent.parent
ARK_URL = 'https://ark.cn-beijing.volces.com/api/v3/chat/completions'
MODEL = os.environ.get('DOUBAO_VISION_MODEL', 'doubao-1-5-vision-pro-32k-250115')
RENDER_DPI = 200
HTTP_TIMEOUT = 90
COST_PER_PAGE = 0.005  # 元

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


def _build_ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


SSL_CTX = _build_ssl_context()


def load_env():
    f = ROOT / '.env'
    if not f.exists(): return
    for raw in f.read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip().lstrip('﻿')
        if line.startswith('export '): line = line[7:].strip()
        if not line or line.startswith('#') or '=' not in line: continue
        k, v = line.split('=', 1)
        v = v.strip()
        if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
            v = v[1:-1]
        os.environ.setdefault(k.strip(), v)


def ocr_page(pdf_path, page_idx, api_key, retries=2):
    """OCR 单页,返回 (text, err)"""
    last_err = None
    for dpi in (RENDER_DPI, 150, 100):
        try:
            doc = fitz.open(pdf_path)
            pix = doc[page_idx].get_pixmap(dpi=dpi)
            png = pix.tobytes('png')
            doc.close()
        except Exception as e:
            return None, f'render failed: {e}'
        if len(png) > 9 * 1024 * 1024 and dpi > 100:
            continue
        b64 = base64.b64encode(png).decode('ascii')

        payload = {
            'model': MODEL,
            'messages': [{
                'role': 'user',
                'content': [
                    {'type': 'text', 'text': OCR_PROMPT},
                    {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{b64}'}},
                ],
            }],
            'temperature': 0.0,
            'max_tokens': 4000,
        }
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(
                    ARK_URL,
                    data=json.dumps(payload).encode('utf-8'),
                    headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'},
                )
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=SSL_CTX) as resp:
                    data = json.loads(resp.read().decode('utf-8'))
                return data['choices'][0]['message']['content'], None
            except urllib.error.HTTPError as e:
                detail = e.read().decode('utf-8', errors='ignore') if e.fp else ''
                last_err = f'HTTP {e.code}: {detail[:300]}'
                if 400 <= e.code < 500 and ('Oversize' in last_err or 'InvalidParameter' in last_err) and dpi > 100:
                    break  # 降 DPI 重试
                if 400 <= e.code < 500:
                    return None, last_err
            except Exception as e:
                last_err = f'{type(e).__name__}: {e}'
            if attempt < retries:
                time.sleep(2 + attempt * 2)
    return None, last_err or 'oversize even at 100 DPI'


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    load_env()
    api_key = os.environ.get('DOUBAO_API_KEY', '').strip()
    if not api_key:
        raise SystemExit('❌ DOUBAO_API_KEY 未配置')

    pdf_path = Path(sys.argv[1]).expanduser()
    if not pdf_path.is_absolute():
        pdf_path = ROOT / pdf_path
    if not pdf_path.exists():
        raise SystemExit(f'❌ PDF 不存在: {pdf_path}')

    doc = fitz.open(pdf_path)
    total = len(doc)
    doc.close()

    start = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    end = int(sys.argv[3]) if len(sys.argv) > 3 else total
    workers = int(sys.argv[4]) if len(sys.argv) > 4 else 4
    end = min(end, total)

    out_dir = ROOT / 'out'
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f'{pdf_path.stem}_OCR.txt'

    page_range = list(range(start - 1, end))  # 0-based
    n_pages = len(page_range)

    print(f'文件: {pdf_path.name}')
    print(f'总页数: {total},本次跑: 第 {start} - {end} 页(共 {n_pages} 页)')
    print(f'并发: {workers}  | 预计成本: ¥{n_pages * COST_PER_PAGE:.2f}  | 预计时间: {n_pages * 7 / workers / 60:.1f}-{n_pages * 12 / workers / 60:.1f} 分钟')
    print(f'输出: {out_file}')
    print()

    results = {}
    completed = 0
    failed = 0
    t0 = time.time()

    def task(page_idx):
        return page_idx, ocr_page(pdf_path, page_idx, api_key)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(task, i) for i in page_range]
        for fut in as_completed(futures):
            page_idx, (text, err) = fut.result()
            results[page_idx] = (text, err)
            completed += 1
            if err:
                failed += 1
                print(f'  [P{page_idx+1:>3d}] ❌ {err[:80]}')
            else:
                elapsed = time.time() - t0
                rate = completed / elapsed
                eta = (n_pages - completed) / max(rate, 0.01)
                print(f'  [{completed:>3d}/{n_pages}] P{page_idx+1:>3d} ok ({len(text or ""):>4d}字) | 已花 ¥{(completed-failed)*COST_PER_PAGE:.2f} | 还需 {eta:.0f}s')

    # 按页序合并写入
    with out_file.open('w', encoding='utf-8') as f:
        f.write(f'# {pdf_path.name}\n# 共 {n_pages} 页(第 {start}-{end}),OCR 完成于 {time.strftime("%Y-%m-%d %H:%M:%S")}\n\n')
        for page_idx in page_range:
            text, err = results.get(page_idx, (None, 'missing'))
            f.write(f'\n━━━━━━━ 第 {page_idx+1} 页 ━━━━━━━\n')
            if err:
                f.write(f'[OCR 失败: {err}]\n')
            else:
                f.write((text or '') + '\n')

    elapsed = time.time() - t0
    print()
    print('═' * 50)
    print(f'完成 {n_pages} 页,耗时 {elapsed/60:.1f} 分钟')
    print(f'成功 {n_pages - failed} 页,失败 {failed} 页')
    print(f'累计成本: ¥{(n_pages - failed) * COST_PER_PAGE:.2f}')
    print(f'输出文件: {out_file}')


if __name__ == '__main__':
    main()
