"""扫描「产品库」目录,生成 data/library_manifest.json(资料下载清单)。

规则:
  - 排除「价格与政策」(含报价,不对业务开放)
  - 只收 PDF(单页 / 中文说明书 / 英文说明书);docx 卖点整理、txt 提取稿不进下载清单
  - 自动按文件名分类,分错的可以事后手动改 JSON 里的 "type" 字段

产品增删后重跑即可:
    python3 scripts/gen_library_manifest.py

注意:生成的 "key" 是相对「产品库」根目录的路径,COS 上的对象 Key 需要
      = COS_PREFIX + key。COS_PREFIX 在服务器 .env 里配(默认 "产品库/")。
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB_DIR = ROOT / "产品库"
OUT = ROOT / "data" / "library_manifest.json"

EXCLUDE_TOP = {"价格与政策"}          # 不对业务开放的大类
ALLOW_EXT = {".pdf"}                  # 只开放 PDF


def classify(fn: str) -> str:
    """按文件名猜类型。分错没关系,JSON 里手动改 type 即可。"""
    n = fn
    if "单页" in n or "画板" in n:
        return "单页"
    eng = (
        "ENG" in n
        or ".EN." in n
        or n.replace(" ", "").upper().endswith("EN.PDF")
        or "Owners Manual" in n
        or "英文" in n
        or "QuickStart.EN" in n
    )
    zh = (
        "簡中" in n or "CHS" in n or "CHT" in n or ".ZH." in n
        or "说明书" in n or "产品详解" in n or "OG" in n
        or ("Print" in n and "ENG" not in n)
    )
    if eng and not zh:
        return "英文说明书"
    if zh and not eng:
        return "中文说明书"
    if "OM" in n or "QSM" in n or "QuickStart" in n:
        return "英文说明书" if eng else "中文说明书"
    # 裸产品名 PDF(如「TX-5跑步机.pdf」「家庭综合力量站X.pdf」)多为单页/宣传页
    return "单页"


# 同一产品里多个文件的展示顺序
TYPE_ORDER = {"单页": 0, "中文说明书": 1, "英文说明书": 2, "其他": 3}


def main():
    if not LIB_DIR.exists():
        raise SystemExit(f"❌ 找不到产品库目录:{LIB_DIR}")

    brands = {}
    total_files = 0

    for brand_dir in sorted(LIB_DIR.iterdir()):
        if not brand_dir.is_dir() or brand_dir.name in EXCLUDE_TOP:
            continue
        brand = brand_dir.name
        products = []
        for prod_dir in sorted(brand_dir.iterdir()):
            if not prod_dir.is_dir():
                continue
            files = []
            for f in sorted(prod_dir.iterdir()):
                if not f.is_file() or f.name.startswith("."):
                    continue
                if f.suffix.lower() not in ALLOW_EXT:
                    continue
                rel = f.relative_to(LIB_DIR).as_posix()  # 乔山Johnson/.../xxx.pdf
                files.append({
                    "name": f.name,
                    "type": classify(f.name),
                    "key": rel,
                    "size": f.stat().st_size,
                })
            if not files:
                continue
            files.sort(key=lambda x: (TYPE_ORDER.get(x["type"], 9), x["name"]))
            products.append({"name": prod_dir.name, "files": files})
            total_files += len(files)
        if products:
            brands[brand] = products

    manifest = {"generated_from": "产品库", "brands": brands}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    n_products = sum(len(v) for v in brands.values())
    print(f"✓ 已生成 {OUT}")
    print(f"  品牌 {len(brands)} 个 / 产品 {n_products} 款 / 文件 {total_files} 份")
    for b, ps in brands.items():
        print(f"  - {b}: {len(ps)} 款")


if __name__ == "__main__":
    main()
