"""校验 COS 配置是否正确:逐个检查 manifest 里的 key 在桶里是否真实存在。

用途:上传完产品库、配好 .env 之后,在服务器上跑一次,确认
  COS_PREFIX 设对了、文件都传上去了、密钥能读。

用法(服务器):
    cd /home/ubuntu/xiaohongshu-JohnsonAgent
    set -a && . ./.env && set +a        # 载入 .env 里的 COS_* 变量
    python3 scripts/verify_cos.py

输出:命中 / 缺失 数量,并列出前若干个缺失的 key 方便排查。
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "library_manifest.json"


def main():
    sid = os.environ.get("COS_SECRET_ID", "").strip()
    skey = os.environ.get("COS_SECRET_KEY", "").strip()
    region = os.environ.get("COS_REGION", "").strip()
    bucket = os.environ.get("COS_BUCKET", "").strip()
    prefix = os.environ.get("COS_PREFIX", "产品库/")

    missing_cfg = [k for k, v in {
        "COS_SECRET_ID": sid, "COS_SECRET_KEY": skey,
        "COS_REGION": region, "COS_BUCKET": bucket,
    }.items() if not v]
    if missing_cfg:
        print("❌ 这些环境变量没读到:", ", ".join(missing_cfg))
        print("   提示:先执行  set -a && . ./.env && set +a  再跑本脚本")
        sys.exit(1)

    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError:
        print("❌ 缺依赖,请先:pip3 install cos-python-sdk-v5 --break-system-packages")
        sys.exit(1)

    client = CosS3Client(CosConfig(Region=region, SecretId=sid, SecretKey=skey, Scheme="https"))
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    keys = []
    for products in (manifest.get("brands") or {}).values():
        for p in products:
            for f in p.get("files", []):
                keys.append(f["key"])

    print(f"桶:{bucket} | 地域:{region} | 前缀:{prefix!r}")
    print(f"待校验文件:{len(keys)} 份\n")

    ok, miss = 0, []
    for k in keys:
        object_key = f"{prefix}{k}"
        try:
            client.head_object(Bucket=bucket, Key=object_key)
            ok += 1
        except Exception:
            miss.append(object_key)

    print(f"✓ 命中:{ok}    ✗ 缺失:{len(miss)}")
    if miss:
        print("\n缺失示例(最多 15 条):")
        for m in miss[:15]:
            print("  -", m)
        print("\n排查方向:")
        print("  1) COS_PREFIX 是否和你上传时的路径一致?(整个文件夹上传 → 「产品库/」)")
        print("  2) 文件名是否和本地完全一致?(空格、括号、简繁体都算)")
        print("  3) 桶 / 地域是否填对?")
        sys.exit(2)
    print("\n全部命中,资料库下载功能可用 ✅")


if __name__ == "__main__":
    main()
