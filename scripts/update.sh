#!/bin/bash
# 一键更新产品并推到线上的脚本
# 用法: bash scripts/update.sh "更新说明,如:新增动感单车 C7"

set -e

cd "$(dirname "$0")/.."

# 1. 重新提取产品资料
echo "📦 步骤 1/3: 提取产品资料..."
python3 scripts/build_products.py

# 2. git 提交
COMMIT_MSG="${1:-update products}"
echo ""
echo "📝 步骤 2/3: 提交变更 [$COMMIT_MSG]"
git add .
if git diff --cached --quiet; then
  echo "  (没有任何变更需要提交,可能产品资料没改动)"
  exit 0
fi
git commit -m "$COMMIT_MSG"

# 3. 推送
echo ""
echo "🚀 步骤 3/3: 推送到 GitHub(触发 Vercel 自动部署)..."
git push

echo ""
echo "✅ 完成!约 1 分钟后线上自动更新。"
echo "   去 Vercel Deployments 页面看最新一次部署状态。"
