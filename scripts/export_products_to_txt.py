"""把 manuals 表里的全部产品资料,**按产品**各导出一份 txt,便于人工通读校对。

用法:
    python3 scripts/export_products_to_txt.py

输出:
    out/产品资料导出/
      ├─ 乔山Johnson/
      │   ├─ 智能跑步机TX-5.txt
      │   ├─ 智能跑步机TX3.txt
      │   ├─ ...
      ├─ 搏飞BowFlex/
      │   ├─ 高性能跑步机T6.txt
      │   ├─ ...
      └─ 十字星Schwinn/
          └─ 风阻单车AD6i.txt

每个 txt 里按【卖点整理 → 产品单页 → 说明书】顺序拼接,带清晰分隔标识。
"""
import os
import sqlite3
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / 'data' / 'usage.db'
OUT_DIR = ROOT / 'out' / '产品资料导出'

# 类型显示名 + 排序权重(数字小排前面)
TYPE_LABEL = {
    'selling_docx':  ('卖点整理',  1),
    'onepager_pdf':  ('产品单页',  2),
    'manual_pdf':    ('产品说明书', 3),
    'other_pdf':     ('其他资料',  9),
    'other_docx':    ('其他资料',  9),
}


def safe_name(name: str) -> str:
    """文件名去掉操作系统不允许字符"""
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip()


def main():
    if not DB_PATH.exists():
        raise SystemExit(f'❌ 数据库不存在:{DB_PATH}')

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    # 先查出所有 (brand, product) 组合
    products = conn.execute(
        "SELECT brand, product, COUNT(*) chunks, SUM(char_count) chars "
        "FROM manuals GROUP BY brand, product ORDER BY brand, product"
    ).fetchall()

    if not products:
        print('manuals 表里没数据')
        return

    print(f'共 {len(products)} 个产品要导出')
    print(f'输出目录:{OUT_DIR}')
    print()

    total_chars = 0
    for brand, product, n_chunks, n_chars in products:
        # 一个产品的所有内容
        rows = conn.execute(
            "SELECT source_type, source_file, page_no, content "
            "FROM manuals WHERE brand=? AND product=? "
            "ORDER BY source_type, source_file, page_no",
            (brand, product)
        ).fetchall()

        # 按 source_type 分组,按 TYPE_LABEL 排序权重排
        by_type = {}
        for st, sf, pg, ct in rows:
            by_type.setdefault(st, []).append({
                'source_file': sf, 'page_no': pg, 'content': ct
            })
        sorted_types = sorted(
            by_type.keys(),
            key=lambda t: TYPE_LABEL.get(t, ('其他', 9))[1]
        )

        # 拼成一个 txt
        out_brand_dir = OUT_DIR / safe_name(brand)
        out_brand_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_brand_dir / f'{safe_name(product)}.txt'

        lines = []
        lines.append('═' * 70)
        lines.append(f'  {brand} / {product}')
        lines.append(f'  共 {n_chunks} 段 / {n_chars:,} 字')
        lines.append('═' * 70)
        lines.append('')

        for st in sorted_types:
            label = TYPE_LABEL.get(st, (st, 9))[0]
            items = by_type[st]
            lines.append('')
            lines.append('━' * 70)
            lines.append(f'  【{label}】({len(items)} 段)')
            lines.append('━' * 70)

            # 按文件名 group,文件内按页码排序
            files = {}
            for it in items:
                files.setdefault(it['source_file'], []).append(it)
            for fname, pages in files.items():
                pages.sort(key=lambda x: x['page_no'] or 0)
                lines.append('')
                lines.append(f'──── 文件:{fname} ────')
                for p in pages:
                    if len(pages) > 1 and p['page_no']:
                        lines.append('')
                        lines.append(f'[ 第 {p["page_no"]} 页 ]')
                    lines.append(p['content'])

        out_file.write_text('\n'.join(lines), encoding='utf-8')
        total_chars += n_chars
        print(f'  ✓ {brand}/{product}  →  {len(rows)} 段, {n_chars:,} 字')

    conn.close()

    print()
    print('═' * 50)
    print(f'完成,共 {len(products)} 个产品 / {total_chars:,} 字')
    print(f'输出根目录:{OUT_DIR}')


if __name__ == '__main__':
    main()
