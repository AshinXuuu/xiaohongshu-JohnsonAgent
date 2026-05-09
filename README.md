# 小红书文案生成 Agent

一个为业务同事打造的内部文案生成工具:
**选品牌 → 选产品 → 选文案类型 → 一键生成标题 + 正文 + 话题标签**,直接复制粘贴发小红书。

---

## 功能特点

- 产品资料统一沉淀在 `产品库/`,新增产品只需放进对应文件夹
- 6 种文案类型:种草/场景/生活/促销/干货/封面金句,每种独立 prompt 调优
- 输出标题 5 个备选 + 正文(自带 emoji 排版)+ 话题标签,可一键复制
- 部署到 Vercel 全球访问,业务同事打开网页即用

---

## 项目结构

```
红书文案agent/
├── 产品库/                  # 业务同事维护:每个品牌一个文件夹
│   ├── 乔山Johnson/
│   │   ├── 品牌资料/        # 品牌指南(可选,目前不参与生成)
│   │   └── TX-5智能跑步机/
│   │       ├── 卖点整理.docx   # ⭐ 关键文件,文案生成主要依赖
│   │       └── 产品单页.pdf
│   └── 搏飞BowFlex/...
├── data/products.json       # 自动生成的结构化数据(由脚本提取)
├── prompts/                 # 各文案类型的 prompt 模板,可调优
├── api/                     # Vercel Serverless Functions
│   ├── products.py            # GET /api/products
│   └── generate.py            # POST /api/generate
├── public/index.html        # 前端页面
├── scripts/
│   ├── build_products.py    # 把产品库提取成 products.json
│   └── dev_server.py        # 本地试运行服务器
├── requirements.txt
├── vercel.json
├── .env.example
└── README.md
```

---

## 快速开始(本地)

### 1. 申请 DeepSeek API Key

去 https://platform.deepseek.com/api_keys 注册并创建 key,充值几元就够测试很久。

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env,把 sk-xxxx 替换成真实 key
```

### 4. 提取产品资料

```bash
python scripts/build_products.py
```

每次新增/修改 `产品库/` 下的 docx/pdf 都需要重跑这一步。

### 5. 本地启动

```bash
python scripts/dev_server.py
```

浏览器打开 http://localhost:8000 即可使用。

---

## 部署到 Vercel(免费,推荐)

### 1. 把项目推到 GitHub

```bash
cd "红书文案agent"
git init
git add .
git commit -m "init"
# 在 GitHub 新建仓库后:
git remote add origin <你的仓库地址>
git push -u origin main
```

### 2. 导入到 Vercel

1. 访问 https://vercel.com/new
2. 选择刚才的 GitHub 仓库,点 **Import**
3. **Framework Preset** 选 `Other`,其他默认
4. 在 **Environment Variables** 添加:
   - Name: `DEEPSEEK_API_KEY`
   - Value: 你的 DeepSeek key
5. 点 **Deploy**

约 30 秒后会拿到一个 `xxx.vercel.app` 域名,把这个链接发给业务同事即可。

### 3. 后续更新流程

业务同事新增了产品资料后:

```bash
# 把新的 docx/pdf 放进 产品库/品牌名/产品名/
python scripts/build_products.py
git add .
git commit -m "add product XXX"
git push
```

Vercel 检测到 push 会自动重新部署,1 分钟内生效。

---

## 业务同事新增产品的标准流程

每个产品文件夹下**至少要有一份卖点整理 .docx 文件**(文案生成主要依赖它)。

参考现有的 `TX-5卖点整理.docx`、`M6卖点整理.docx`,推荐结构:

```
产品名(如 TX-5智能跑步机)
核心定位:一句话讲清产品定位

卖点 1:[卖点名](括号里加一句概括)
[展开 2-4 行,讲场景、讲体感、讲数据]

卖点 2:...
...
```

> **注意**:产品 PDF 如果是扫描件,脚本无法提取文字,所以 docx 是必须的。
> PDF 可以放着不管,主要是给业务同事自己查阅用。

---

## 调优文案风格

如果觉得某种类型的文案输出风格不太对,直接改 `prompts/` 下对应的 .txt 文件即可,不需要改代码。

例如:
- `prompts/种草.txt` → 调种草文风格
- `prompts/封面金句.txt` → 调短标题风格
- `prompts/base.txt` → 改总写作原则(对所有类型生效)

改完后:
- 本地测试:重启 `dev_server.py` 即可
- 线上更新:`git add prompts/ && git commit && git push`,Vercel 自动重新部署

---

## 常见问题

**Q1. 生成结果不符合预期怎么办?**
- 第一招:点页面上的"重新生成一版"按钮,模型每次输出都不同
- 第二招:在"补充信息"框里加更具体的指引,如"主打小户型用户""强调静音"
- 第三招:改 `prompts/<类型>.txt`,加更具体的写作要求

**Q2. 想加新品牌或产品怎么办?**
1. 在 `产品库/` 下建对应文件夹
2. 放入卖点整理 docx
3. 跑 `python scripts/build_products.py`
4. push 到 GitHub,Vercel 自动部署

**Q3. 想换模型(从 DeepSeek 换成 Kimi)怎么办?**
改 `api/generate.py` 里的 `call_deepseek` 函数:
- URL 改成:`https://api.moonshot.cn/v1/chat/completions`
- model 改成:`moonshot-v1-32k`
- 环境变量加 `MOONSHOT_API_KEY`

**Q4. 担心 API key 暴露?**
- key 存在 Vercel 的环境变量里,前端代码里看不到
- 前端只能调用 `/api/generate`,真正的 key 调用发生在服务端
- 如果担心被滥用,可以加 IP 白名单或简单 token 鉴权(后续优化)

---

## 后续可优化方向

- [ ] 加简单的访问鉴权(防止链接被外泄滥用 API)
- [ ] 把生成历史保存下来,方便对比
- [ ] 支持多模型切换(界面上切 DeepSeek / Kimi)
- [ ] 支持上传图片让 AI 看图生文
- [ ] 给业务同事一个"上传 docx 自动入库"的页面,免去 git 流程
