# 清理 git 历史里的明文身份证后6位

**背景**:`data/users.json` 曾以明文存储员工身份证后6位,并被 git 跟踪。
现在当前版本已改为加盐哈希(见 `lib/idhash.py`),但**旧的历史提交里仍是明文**。
本文档指导如何把明文从整个 git 历史中彻底抹掉。

> ⚠️ 这一步会**重写整个 git 历史**。所有人手上的旧 clone 都会失效,必须删掉重新 clone。
> 请挑一个没人在推代码的时间窗口执行,并提前通知协作者。

---

## 前置确认

1. 确认当前工作区的 `data/users.json` 已经是哈希版本(字段值形如 `h1$xxxx`,不再是6位数字):
   ```bash
   grep -o '"id_last6": "[^"]*"' data/users.json | head
   # 应看到 h1$... 或空串,不应看到 6 位数字/带 X 的明文
   ```
2. 确认已经有备份(迁移时已自动生成):
   ```bash
   ls backups/users.json.pre-hash.* backups/usage.db.pre-hash.*
   ```

---

## 步骤

### 0) 先把当前哈希版本提交上去

```bash
git add data/users.json lib/idhash.py lib/users_store.py api/login.py public/admin-users.html
git commit -m "身份证后6位改加盐哈希 + 迁移历史数据"
```

### 1) 安装 git-filter-repo(比 filter-branch 更稳、更快)

```bash
pip install git-filter-repo
# 或(部分环境):pip install --break-system-packages git-filter-repo
```

### 2) 从所有历史提交中移除该文件的历史内容

```bash
# 把 data/users.json 从全部历史里剔除(--invert-paths = 只保留其它路径)
git filter-repo --path data/users.json --invert-paths --force
```

执行后,历史里将不再包含 `data/users.json` 的任何版本(包括明文的旧版本)。

### 3) 把当前(哈希版)文件重新加回来

因为上一步把这个文件从历史里连当前版本一起删了,需要用备份/工作区里的哈希版重新提交:

```bash
# 若工作区还在(通常在),直接:
git add data/users.json
git commit -m "restore hashed users.json"

# 若工作区的文件也没了,从备份恢复后再改成哈希版本(备份是明文!需重新迁移):
#   见项目里的迁移逻辑 lib/idhash.py,不要直接把明文备份提交回去。
```

> 注:git-filter-repo 默认会移除 `origin` remote(防止误推)。下一步要先加回来。

### 4) 强推 + 通知团队重新 clone

```bash
# 重新绑定远端(filter-repo 会清掉,按你的实际地址填)
git remote add origin <你的仓库地址>

# 强推所有分支和标签
git push origin --force --all
git push origin --force --tags
```

**通知所有协作者**:历史已重写,旧 clone 不能再 `git pull`。请他们:
```bash
# 删掉旧目录,重新 clone
rm -rf 旧目录 && git clone <仓库地址>
```

---

## 部署侧同步(服务器)

服务器上的 `data/usage.db` 是线上真实库,`git pull` 不会迁移它。二选一:

- **办法 A(简单)**:`git pull` 后进后台 → 用户管理 → 点「从名单重新导入」。
  当前 `users.json` 已是哈希,重导入会用哈希覆盖 DB。
- **办法 B**:在服务器上对 `data/usage.db` 跑一次明文→哈希迁移(同迁移逻辑)。

并确保服务器 `.env` 里有**与本地一致的** `ID6_SALT`(否则已存哈希验不过,员工登不进)。

最后:
```bash
sudo systemctl restart agent
```

---

## 验证

- 用一个已配置后6位的员工账号登录:填对 → 成功,填错 → 拒绝。
- 后台「用户管理」列表:身份证列显示「已设置 / 未设置」,不再显示明文。
- 历史检查(应无明文):
  ```bash
  git log --all -p -- data/users.json | grep -E '"id_last6": "[0-9]{5}[0-9Xx]"' | head
  # 无输出 = 干净
  ```
