# Canva 集成首次设置手册

整套集成已经编码完成,你需要做的就是**走 6 步配置**,之后业务点封面按钮就能直接跳进 Canva 编辑页。

预计总耗时:**30-40 分钟**(主要花在 Canva 里设计第一个模板上)。

---

## Step 1:Vercel 加 3 个环境变量

打开 https://vercel.com/dashboard → 选你的项目 → **Settings → Environment Variables**

添加这 3 条(三个 Environment 都勾选):

| Key | Value(从 Canva Developer 后台复制) |
|---|---|
| `CANVA_CLIENT_ID` | 你的 Client ID(`OC-` 开头那一串) |
| `CANVA_CLIENT_SECRET` | 你的 Client Secret(在 Canva 后台 Configure 页生成) |
| `CANVA_REDIRECT_URI` | `https://你的vercel域名.vercel.app/api/canva-callback` |

> Client ID 和 Secret 都在 Canva Developer 后台你的 integration 的 **Configure → OAuth 2.0 credentials** 里。
> Secret 只显示一次,如果忘了点 **Regenerate**(旧的会立即失效)。

> ⚠️ **重要**:Client Secret 是敏感凭证,**只放在 Vercel 环境变量和本地 .env 里,绝对不要写进任何会进 GitHub 的文件**(包括 README、注释、代码)。

---

## Step 2:把新代码推到 GitHub

终端跑:

```bash
cd ~/Desktop/红书文案agent
git add .
git commit -m "Canva Connect 集成:支持一键跳转编辑封面"
git push
```

Vercel 会自动重新部署,等到 ✓ Ready,**约 1 分钟**。

---

## Step 3:走一次 OAuth 授权,拿到 Refresh Token

**用你/营销同事日常使用的 Canva 账号**做这一步(这个账号会承载所有自动生成的设计,所以要选大家都能用的)。

1. 浏览器打开:`https://xiaohongshu-johnson-agent.vercel.app/api/canva-auth`
2. 跳到 Canva 授权页,点 **Allow / 同意**
3. 跳回来后看到 **"✅ Canva 授权成功"** 页面,上面有一段 `Refresh Token`
4. **点"点击复制"按钮**,把整串复制下来

---

## Step 4:把 Refresh Token 存到 Vercel

回 Vercel **Settings → Environment Variables**,再加 1 条:

| Key | Value |
|---|---|
| `CANVA_REFRESH_TOKEN` | (粘贴你刚才复制的那一长串) |

三个 Environment 都勾选 → Save。

然后 **Deployments → 最新一次 Redeploy**(不勾 cache),等 ✓ Ready,约 1 分钟。

到这步,**OAuth 配置就永久搞定了**,refresh token 有效期约 1 年。

---

## Step 5:在 Canva 里设计品牌模板(关键一步)

这是整个流程"颜值天花板"所在,认真做。

### 5.1 创建模板

1. Canva 里点 **创建设计** → 选 **自定义尺寸** → 输 `900x1200`(小红书 3:4 封面)
2. 设计你的封面(参考你之前发我那些种草/避雷风格的图)
3. 设计完毕后,**重点:把它转成"品牌模板"**:
   - 右上角点 **Share / 分享** → **Brand Templates / 品牌模板**
   - 或者直接复制到 Brand Hub(需要 Canva Pro 团队版)

### 5.2 设置"命名占位符"(整套自动化的关键)

为了让 API 能往里填字和图,**必须给特定元素命名**:

#### 给文字框命名:
1. 点中你的"主标题"文字框
2. 顶部菜单出现 ⋯ (更多) 按钮 → 点开
3. 选 **Toggle data field** / **设为数据字段**
4. **字段名输入** `title`(全小写,不能错)

> 如果你模板还有副标题:再选副标题文字框,同样操作,字段名设为 `subtitle`

#### 给图片占位符命名:
1. 在模板上放一个图片占位区(可以拖一张占位图进去)
2. 点中那张图
3. ⋯ (更多) → **Toggle data field** / **设为数据字段**
4. **字段名输入** `product_image`(全小写,不能错)

### 5.3 关于命名,补充说明

我们后端会把这些字段往里填:

| 字段名 | 必填? | 内容 |
|---|---|---|
| `title` | ✅ 必填 | 业务从 5 个候选标题里选的那一条 |
| `product_image` | ✅ 必填 | 产品库里对应的 `封面图.jpg` |
| `subtitle` | 选填 | 副标题(目前前端没暴露,如果模板里有,会留空) |
| `tag` | 选填 | 角标(同上) |

模板里**没有的字段不会被填**(也不会报错)。你可以**先做一个简单模板**测试,跑通后再回头优化。

### 5.4 保存

模板放进 Brand Templates 后,**自动可被 API 访问**,不用再做额外发布动作。

---

## Step 6:回网站测试

刷新你的 vercel 网站 → 选一个产品 → 生成文案 → 滚到底部应该出现 **"🎨 一键去 Canva 做封面"** 区块:

1. 选一个主标题(默认选第一个)
2. **模板下拉里应该看到你刚才在 Canva 设计的模板**(如果没看到,等 1-2 分钟刷新一下,Canva 模板缓存)
3. 点中模板卡片
4. 点 **🎨 生成封面并跳转 Canva**
5. 等 10-30 秒,会弹出新标签页,**已经填好图和字的 Canva 设计**
6. 你微调(改字体大小、挪挪元素、加几个 emoji),然后点 Canva 的 **Share → Download → PNG** 导出

---

## 验证清单

| 检查项 | 看到什么 = 成功 |
|---|---|
| Vercel 部署 | Deployments 最新行有 ✓ Ready |
| OAuth 授权 | `/api/canva-callback` 页面显示 "✅ Canva 授权成功" |
| 模板列表加载 | 网站封面模块里看到至少 1 张模板缩略图 |
| 自动填充成功 | 点生成 → 跳转新标签页 → Canva 里看到产品图和标题已填好 |

---

## 后续运营 SOP

**业务同事日常使用**:跟之前一样,生成文案 → 选标题 → 选模板 → 跳转 Canva → 微调导出。**他们不需要任何 Canva 知识**,只需要会用 Canva 编辑器调整。

**管理员日常**:基本无操作。如果想换/加模板,只在 Canva 里改模板,会自动反映在网页上。如果 1 年后 refresh token 失效,重走 Step 3-4 即可。

**新增产品时**:产品库里加文件夹 + 卖点 docx + **`封面图.jpg`(900x1200 或 3:4 比例,文件名严格一致)** → 跑 `update.sh` 推送 → 完成。

---

## 故障排查

| 报错 | 原因 | 解决 |
|---|---|---|
| Canva 模板加载失败,503 | refresh_token 没配 | 回 Step 3-4 |
| Canva API 错误 401 | refresh_token 过期或失效 | 重走 Step 3,拿新 token 更新 Vercel env |
| Canva API 错误 403 | scope 权限不够 | 在 Canva Developer 后台勾上所有 brand-template + design + asset scope,重新走 OAuth |
| `找不到产品图` | 没在产品文件夹放 封面图.jpg | 按 Step 6 命名要求放置 |
| 跳转后 Canva 显示没有 title/product_image | 模板里没设这两个数据字段 | 回 Step 5.2 重新设占位符命名 |
