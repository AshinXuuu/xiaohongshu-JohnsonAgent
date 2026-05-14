# 小红书文案 Agent · 云部署完整手册(Lighthouse 实战版)

> 这份文档由"踩坑后总结"出来的实战经验,不是理论清单。包含完整部署流程 + 12 个真实问题的解决方案。

---

## 目录

1. [一、架构总览](#一架构总览)
2. [二、前置准备清单](#二前置准备清单)
3. [三、完整部署 10 步](#三完整部署-10-步)
4. [四、本项目真实踩过的 12 个坑](#四本项目真实踩过的-12-个坑)
5. [五、日常维护手册](#五日常维护手册)
6. [六、文件结构和接口清单](#六文件结构和接口清单)
7. [七、未来优化方向](#七未来优化方向)

---

## 一、架构总览

```
[业务浏览器(国内,无需 VPN)]
        ↓ HTTPS
[域名:mkt1.johnsonfitness.com.cn / 待备案的新域名]
        ↓ DNS A 记录
[腾讯云轻量服务器 Lighthouse(上海,Ubuntu 22.04)]
        ├─ Nginx(端口 443/80)
        │   └─ 反向代理到 127.0.0.1:8000
        ├─ Python systemd 服务(端口 8000)
        │   └─ dev_server.py 动态路由所有 api/*.py
        └─ Let's Encrypt 自动续期 HTTPS 证书

[外部依赖]
  ├─ DeepSeek API(文案生成)
  ├─ 火山引擎豆包 Seedream API(封面生成)
  └─ Upstash Redis(KV 持久化用量日志)
```

**为什么不用 Vercel + EdgeOne?**

走过弯路才知道的真相:**Vercel 在境外,EdgeOne 个人版国内回源到境外稳定性极差**,实测 522 错误高发。最终方案是把整个后端搬到上海 Lighthouse,做到"国内闭环",**业务从任意国内网络访问都是秒开**。

---

## 二、前置准备清单

部署前确认你有:

| 项 | 说明 | 备注 |
|---|---|---|
| 腾讯云账号 | 实名认证完成 | 企业实名更稳 |
| 已备案域名 | `johnsonfitness.com.cn` 或新备案的 | 子域名继承备案,无需额外申请 |
| GitHub 仓库访问 | 含 PAT(Personal Access Token) | 用于 clone 私有仓库 |
| **DeepSeek API key** | `sk-` 开头 | https://platform.deepseek.com |
| **豆包 API key** | 火山引擎 Ark 平台 | https://console.volcengine.com/ark |
| Upstash Redis | `KV_REST_API_URL` + `KV_REST_API_TOKEN` | Vercel KV 集成或独立 Upstash 账号 |
| Lighthouse 服务器 | **2 核 4GB 起,Ubuntu 22.04 LTS** | 上海/广州/北京任选,**别选香港**(不支持备案) |

> **关于 Lighthouse 选型**:**不要选 40 元那档(2GB 内存)**——Python + Nginx + 业务+ 可能的 OpenClaw 等会 OOM。**65 元/月(2 核 4GB)** 是最佳性价比。

---

## 三、完整部署 10 步

### Step 1:登录 Lighthouse

**腾讯云控制台 → 轻量应用服务器 → 实例列表 → 点中实例卡片 → 顶部"登录"按钮 → WebSSH 自动打开**

不需要装 SSH 客户端,**浏览器里直接用**。看到 `ubuntu@VM-x-x:~$` 提示符就是成功了。

> 默认用户:`ubuntu`(如果你的镜像是 root 模式,后面所有 `/home/ubuntu/` 改成 `/root/`)。

---

### Step 2:基础环境(关键:nginx 必须装上)

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git nginx certbot python3-certbot-nginx ufw
sudo pip3 install python-docx pypdf --break-system-packages
```

**关键验证(每条都跑一遍,任一不通就重装)**:

```bash
python3 --version    # 应该 Python 3.10.x
nginx -v             # 应该 nginx version: nginx/1.18.x
git --version
sudo systemctl status nginx | head -5   # 必须 active (running)
```

> ⚠️ **真实踩过的坑**:apt 偶尔会**静默跳过 nginx** 安装(尤其是 apt update 报错时)。**必须用上面命令逐一验证版本号**,**不能假设装好了**。

---

### Step 3:Clone 代码(跨境网络坑,必须特殊处理)

#### 3.1 配置 git

```bash
git config --global user.email "your_email"
git config --global user.name "your_github_username"
git config --global http.postBuffer 524288000
git config --global http.lowSpeedLimit 0
git config --global http.lowSpeedTime 999999
git config --global http.version HTTP/1.1
```

#### 3.2 准备 GitHub Personal Access Token

1. 浏览器打开 https://github.com/settings/tokens
2. **Generate new token (classic)**
3. Note:`lighthouse-deploy`,Expiration:90 days,Scopes 勾 **`repo`**
4. 复制 `ghp_xxxx` 那一长串,**只显示一次,马上记下**

#### 3.3 Clone(用浅克隆 + 自动重试)

```bash
cd ~
git clone --depth 1 https://YOUR_GITHUB_USER:ghp_xxxxxxxx@github.com/YOUR_GITHUB_USER/xiaohongshu-JohnsonAgent.git
```

> ⚠️ **如果 GnuTLS 报错**(TLS connection non-properly terminated):
> ```bash
> # 走 GitHub 国内代理
> git clone --depth 1 https://YOUR_USER:ghp_xxx@ghproxy.com/https://github.com/YOUR_USER/xiaohongshu-JohnsonAgent.git
> ```

#### 3.4 验证 + 立刻作废 PAT

```bash
ls -la ~/xiaohongshu-JohnsonAgent
# 应该看到 api/ data/ prompts/ public/ scripts/ 等
```

**关键安全步骤**:Clone 完后,**立刻回 GitHub Tokens 页面删掉这个 PAT**。它已经留在 `.bash_history` 和命令历史里,严格意义算泄漏。

> 长期维护推荐用 **SSH key 替代 PAT**:`ssh-keygen → 公钥加到 GitHub Deploy Keys → 改用 `git@github.com:...` 协议 clone`。

---

### Step 4:配置环境变量

```bash
cd ~/xiaohongshu-JohnsonAgent
nano .env
```

粘贴(替换为真实 key):

```
DEEPSEEK_API_KEY=sk-xxx
DOUBAO_API_KEY=xxx
DOUBAO_MODEL=doubao-seedream-5-0-lite-260128
DOUBAO_IMAGE_SIZE=1920x2560
KV_REST_API_URL=https://xxx.upstash.io
KV_REST_API_TOKEN=xxx
```

`Ctrl+O` → `Enter` → `Ctrl+X` 保存退出。

```bash
chmod 600 ~/xiaohongshu-JohnsonAgent/.env   # 锁权限
grep -c "=" ~/xiaohongshu-JohnsonAgent/.env  # 应该输出 ≥ 5
```

> ⚠️ **关于 DOUBAO_IMAGE_SIZE**:必须 `1920x2560`(或更大),**不能用 768x1024**——豆包 Seedream 5.0 lite 要求输出至少 3,686,400 像素,小尺寸会报 `InvalidParameter`。

---

### Step 5:测试 Python 服务能跑

```bash
cd ~/xiaohongshu-JohnsonAgent
set -a; source .env; set +a
python3 scripts/dev_server.py
```

看到:
```
🚀 服务已启动: http://0.0.0.0:8000
   注册的 API 端点 (6 个):
     - /api/admin-stats
     - /api/cover-fields
     - /api/cover-generate
     - /api/generate
     - /api/login
     - /api/products
```

**新开一个 WebSSH 标签页验证**:

```bash
curl http://localhost:8000/api/login
# 应该返回长 JSON,含 departments 和 users_by_dept
```

回原来标签页 `Ctrl+C` 停掉测试服务。

> ⚠️ **如果 dev_server 只列出 2 个端点**:你 clone 的是老版本代码,**git pull 拉最新**。`dev_server.py` 必须是"动态路由版"才能识别所有 API。

---

### Step 6:systemd 后台常驻

```bash
sudo nano /etc/systemd/system/agent.service
```

粘贴(注意如果用户是 root,把 `/home/ubuntu/` 改成 `/root/`):

```ini
[Unit]
Description=Xiaohongshu Agent
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/xiaohongshu-JohnsonAgent
EnvironmentFile=/home/ubuntu/xiaohongshu-JohnsonAgent/.env
ExecStart=/usr/bin/python3 /home/ubuntu/xiaohongshu-JohnsonAgent/scripts/dev_server.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

启动:

```bash
sudo systemctl daemon-reload
sudo systemctl enable agent
sudo systemctl start agent
sudo systemctl status agent | head -10  # 应该 active (running)
```

---

### Step 7:Nginx 反向代理

```bash
sudo nano /etc/nginx/sites-available/agent
```

粘贴:

```nginx
server {
    listen 80;
    server_name mkt1.johnsonfitness.com.cn;

    client_max_body_size 10M;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
        proxy_connect_timeout 30s;
        proxy_send_timeout 60s;
    }
}
```

启用:

```bash
sudo ln -sf /etc/nginx/sites-available/agent /etc/nginx/sites-enabled/agent
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t                            # 必须显示 syntax is ok
sudo systemctl reload nginx
```

防火墙(顺序重要,`enable` 前必须先 `allow 22`):

```bash
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable
```

**还要在腾讯云控制台开端口**:实例详情页 → **防火墙** 标签 → 添加 **HTTP(80)** 和 **HTTPS(443)** 规则。

> ⚠️ 实例列表页是看不到"防火墙"标签的,**必须点中实例卡片进详情页**。

验证:

```bash
curl -H "Host: mkt1.johnsonfitness.com.cn" http://localhost/api/login
# 应该返回 JSON,不是 502
```

---

### Step 8:DNS 切换到 Lighthouse 公网 IP

腾讯云控制台 → Lighthouse 实例详情 → 复制公网 IP(如 `124.222.164.101`)。

进你公司域名 DNS 后台(DNSPod / 阿里云 / 华为云):

- **删除**旧的 `mkt1` CNAME(指向 EdgeOne 那条)
- **新增** A 记录:
  - 主机记录:`mkt1`
  - 记录类型:`A`
  - 记录值:你的 Lighthouse 公网 IP
  - TTL:`600`

等 5-10 分钟生效。验证:

```bash
dig mkt1.johnsonfitness.com.cn +short
# 应该返回 Lighthouse 公网 IP
```

---

### Step 9:HTTPS 证书(Let's Encrypt 免费)

DNS 生效后才能跑(certbot 会去验证域名所有权):

```bash
sudo certbot --nginx -d mkt1.johnsonfitness.com.cn
```

回答:
- Email:你的邮箱
- Agree:`A`
- Share email:`N`
- **HTTP redirect**:`2`(自动跳 HTTPS)

证书有效期 90 天,**certbot 会自动续期**,无需人工。

---

### Step 10:最终验证

浏览器无痕模式访问 `https://mkt1.johnsonfitness.com.cn`:

- ✅ 看到登录页(地址栏 🔒 表示 HTTPS 生效)
- ✅ 部门下拉框有 9 个选项
- ✅ 选了部门后,姓名下拉框联动出现
- ✅ 用 `市场部 / 徐昕 / 888888` 登录
- ✅ 生成文案 / 一键导入 / 上传图 / 生成封面 都跑通

跑通就 🎉。

---

## 四、本项目真实踩过的 12 个坑

### 坑 1:EdgeOne 个人版不能稳定回源到 Vercel(522 错误)

**现象**:EdgeOne 配置完美,但访问域名一直 522 Bad Gateway。

**原因**:Vercel 在美国,EdgeOne 国内节点跨境回源经常超时。**这是网络层问题,任何配置都修不了**。

**解决**:**放弃 Vercel,把代码搬到上海 Lighthouse**。整套架构国内闭环。

### 坑 2:EdgeOne Pages 不支持 Python serverless

**现象**:把 GitHub repo 接到 EdgeOne Pages,部署"成功",但所有 `/api/*` 调用 404。

**原因**:EdgeOne Pages 只支持静态网站 + JavaScript Edge Functions,**不支持 Python**。

**解决**:用 EdgeOne **网站安全加速**(CDN 反代模式),不用 Pages。但本项目最终方案是迁到 Lighthouse,完全绕开 EdgeOne。

### 坑 3:GitHub PAT 泄漏

**现象**:粘贴 git clone 命令时,完整 PAT(`ghp_xxx...`)出现在聊天/截图里。

**风险**:任何看到这条记录的人都能用这个 PAT 操作你的 GitHub repo。

**解决**:**clone 成功后立刻去 https://github.com/settings/tokens 删除**。长期用 SSH key 替代。

### 坑 4:GnuTLS recv error(-110) — GitHub clone 中断

**现象**:
```
fatal: unable to access 'https://github.com/...': GnuTLS recv error (-110)
```

**原因**:上海到 GitHub 美国服务器的 TLS 跨境连接不稳。

**解决**:
1. 加 git 容错配置:`http.postBuffer 524288000`、`http.version HTTP/1.1`
2. **`--depth 1` 浅克隆**(减少传输量)
3. 用国内 GitHub 代理:`ghproxy.com/https://github.com/...`

### 坑 5:Nginx 没装上但 apt 没报错

**现象**:`sudo nginx -t` 报 `command not found`。

**原因**:apt 偶尔静默跳过某个包(尤其网络抽风时)。

**解决**:**每个软件装完都跑 `--version` 验证**,不要假设装好了。重装:`sudo apt install -y nginx`。

### 坑 6:实例列表页找不到"防火墙"标签

**现象**:腾讯云 Lighthouse 实例列表页只能看到"登录"按钮,没有防火墙菜单。

**原因**:防火墙在**实例详情页**,不在列表页。

**解决**:**点中实例卡片本身**(不是登录按钮),进入详情页,顶部就有"防火墙"标签。

### 坑 7:dev_server.py 不识别新 API 端点

**现象**:Step 5 测试时 `curl /api/login` 返回 `Not Found`,但服务在运行。

**原因**:老版 `dev_server.py` 写死了 `/api/products` 和 `/api/generate` 两个路由,后来添加的 `/api/login`、`/api/cover-fields`、`/api/cover-generate`、`/api/admin-stats` 不会被识别。

**解决**:升级到动态路由版 `dev_server.py`,**自动识别 `api/*.py` 所有文件**,以后加新端点零成本。

### 坑 8:Vercel Function 跨境超时(`The write operation timed out`)

**现象**:豆包生成封面,3 张全 502 timeout。Network 显示请求耗时 3 分钟。

**原因**:用户上传的产品图 2.77 MB,base64 后 3.7 MB,**3 张并行上传到豆包**需要 11 MB 跨境带宽,Vercel 美西出口到北京豆包写超时。

**解决**:**前端 canvas 压缩**到 1200px 长边,大约 200-500 KB,减少 80% 流量。代码已实现在 `public/index.html` 的 `compressImage()` 函数。

### 坑 9:豆包模型名错误(`InvalidEndpointOrModel.NotFound`)

**现象**:Doubao API 返回 `The model or endpoint doubao-seededit-3-0-i2i-250628 does not exist`。

**原因**:模型 ID 可能版本不对,或账号下没开通该模型。

**解决**:
- 进火山引擎控制台 → 模型广场 → 看用户实际有权限的模型(本项目最终用 `doubao-seedream-5-0-lite-260128`)
- 注意:**Seedream 是文生图**(不保留原图),**SeedEdit 才是图编辑**(保留原图加文字)。本项目暂用 Seedream lite

### 坑 10:豆包返回 InvalidParameter,size 不够大

**现象**:`image size must be at least 3686400 pixels`。

**原因**:Seedream 5.0 lite 强制要求输出至少 368.64 万像素,默认的 `768x1024` 太小。

**解决**:`DOUBAO_IMAGE_SIZE=1920x2560`(491.5 万像素,3:4 比例)。

### 坑 11:Python 字符串里 `"中文"` 内嵌英文双引号导致 SyntaxError

**现象**:某次提示词改动后,`api/cover-generate.py` 编译失败。

**原因**:在英文双引号字符串里写了 `"花字"` `"上新"` `"特惠"` 等,内部双引号截断了外层字符串。

**解决**:
- 全文用**单引号包字符串**,中文内部的引号一律用 `「」`
- 项目代码已统一改造完成
- 经验:Python 源码涉及中文,**默认单引号包,避免双引号冲突**

### 坑 12:Vercel KV 设置后用了一次就失效(`Token lineage revoked`)

**现象**:Canva Connect API refresh token 第二次调用就失效。

**原因**:Canva refresh token 是**单次使用**的,每次刷新会发新 token,旧的立即作废。

**解决**:必须用持久化存储(Vercel KV / Redis)自动写回新 token。本项目最后从 Canva 自动集成方案降级,改用更简单的"复制金句 + 跳转 Canva"模式,再后来改成 Doubao Seedream 直接生图。

---

## 五、日常维护手册

### 5.1 更新代码

在你 Mac 上:
```bash
cd ~/Desktop/红书文案agent
git add . && git commit -m "更新内容描述" && git push
```

SSH 到 Lighthouse:
```bash
cd ~/xiaohongshu-JohnsonAgent
git pull
sudo systemctl restart agent
```

总耗时 < 1 分钟。

### 5.2 看服务日志

```bash
sudo journalctl -u agent -f       # 实时滚动
sudo journalctl -u agent -n 100   # 最近 100 行
sudo journalctl -u agent --since "1 hour ago"  # 最近 1 小时
```

### 5.3 看用量统计

进 `https://mkt1.johnsonfitness.com.cn` → 徐昕账号登录 → 点 **🛡️ 管理员** 按钮,自带:
- 累计 / 今日 / 本月 操作次数
- 部门排行
- 个人 Top 15
- 操作类型分布
- 风格偏好
- 14 天趋势
- 最近 30 条操作流水

数据来自 Upstash Redis,**永久保留**(免费额度 10000 commands/day,目前用量极低)。

### 5.4 新增产品资料

在 Mac 上 `~/Desktop/红书文案agent/产品库/<品牌>/<产品>/` 下放卖点 docx,然后:

```bash
cd ~/Desktop/红书文案agent
python3 scripts/build_products.py   # 重新生成 data/products.json
git add . && git commit -m "新增 XX 产品" && git push
```

然后 SSH 到 Lighthouse `git pull && sudo systemctl restart agent`。

### 5.5 新增用户(员工)

修改 `data/users.json`,在对应部门数组里 append:
```json
{"name": "新员工", "emp_id": "20260001"}
```

push 到 GitHub → Lighthouse git pull + 重启。新员工立刻能登录。

### 5.6 修改提示词风格

改 `prompts/*.txt`(种草/干货/促销/封面字段 等)或 `api/cover-generate.py` 里的 `STYLE_PROMPT_POOLS`。

push + 重启 → 立刻生效。

### 5.7 SSL 证书

自动续期,正常情况下不用管。如果手动测试续期:
```bash
sudo certbot renew --dry-run
```

---

## 六、文件结构和接口清单

```
xiaohongshu-JohnsonAgent/
├── api/                          # 6 个 Serverless 风格端点
│   ├── login.py                  # POST 登录验证;GET 返回部门姓名列表
│   ├── products.py               # GET 列出所有品牌+产品
│   ├── generate.py               # POST 生成小红书文案
│   ├── cover-fields.py           # POST 一键导入封面三字段
│   ├── cover-generate.py         # POST 调豆包生成 3 张封面图
│   └── admin-stats.py            # POST 管理员后台数据
├── data/
│   ├── products.json             # 产品库(由 build_products.py 生成)
│   └── users.json                # 员工白名单 + 管理员标记
├── prompts/                      # 文案 prompt 模板
│   ├── base.txt                  # 通用写作原则
│   ├── 种草.txt / 场景.txt / 生活.txt / 促销.txt / 干货.txt / 封面金句.txt
│   └── 封面字段.txt              # 一键导入的萃取规则
├── lib/
│   └── kv_store.py               # Upstash Redis 用量日志封装
├── public/
│   └── index.html                # 单页应用(登录 + 主界面 + 历史 + 管理员)
├── scripts/
│   ├── dev_server.py             # 动态路由的开发/生产服务器
│   ├── build_products.py         # 把 产品库/*.docx 解析成 products.json
│   └── update.sh                 # 一键更新脚本
├── 产品库/                       # 产品原始资料(docx + pdf)
│   ├── 乔山Johnson/...
│   └── 搏飞BowFlex/...
├── .env                          # 环境变量(本地;不进 git)
├── .gitignore
├── requirements.txt              # Python 依赖
├── vercel.json                   # 历史遗留,Lighthouse 部署不用
├── LIGHTHOUSE_DEPLOY.md          # 本文档
├── DOUBAO_SETUP.md               # 豆包 API 接入说明
└── README.md
```

### API 端点速查

| 端点 | 方法 | 作用 |
|---|---|---|
| `/api/login` | GET | 返回部门/姓名下拉数据 |
| `/api/login` | POST | 校验白名单,返回用户对象 |
| `/api/products` | GET | 列出所有品牌+产品(给文案生成下拉用) |
| `/api/generate` | POST | 调 DeepSeek 生成 5 标题 + 正文 + 标签 |
| `/api/cover-fields` | POST | 调 DeepSeek 萃取封面 3 字段 |
| `/api/cover-generate` | POST | 调豆包 Seedream 生成 3 张封面图 |
| `/api/admin-stats` | POST | 管理员后台,聚合 KV 数据 |

### 环境变量速查

| Key | 必填 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | ✅ | DeepSeek 平台 |
| `DOUBAO_API_KEY` | ✅ | 火山引擎 Ark |
| `DOUBAO_MODEL` | ⚠️ | 默认 `doubao-seedream-5-0-lite-260128` |
| `DOUBAO_IMAGE_SIZE` | ⚠️ | 默认 `1920x2560`,必须 ≥ 368 万像素 |
| `KV_REST_API_URL` | 选填 | Upstash REST API URL |
| `KV_REST_API_TOKEN` | 选填 | Upstash REST API Token |

---

## 七、未来优化方向

### 短期(随时可做)

- **PIL 文字后期处理**:AI 生封面文字仍偶尔出错。可以让豆包出"无文字底图",PIL 用阿里巴巴普惠体精准叠加文字,**文字 100% 准确**。
- **历史封面存到 KV**:目前历史画廊存在浏览器 localStorage(单设备),换电脑就丢。
- **Prompt 埋点反馈**:让业务点"满意/不满意",回写 KV,后续优化 prompt 时知道哪些变体效果差。

### 中期(需要一些重构)

- **加 Redis 缓存**:相同产品+类型的近期文案缓存,降低 DeepSeek 调用成本。
- **支持 docx 在线编辑**:业务直接在网页编辑产品资料,不用 git push。
- **多语言生成**:针对海外社媒(Instagram/TikTok),英文版小红书风格文案。

### 长期(架构升级)

- **接专业海报生成 API**:升级到 Bannerbear / Placid 等,文字 100% 准确,Canva 级质量。
- **企业微信集成**:文案生成结果直接推送到企微,业务无需打开网站。
- **数据分析仪表盘**:Grafana / Metabase 接 KV,真实业务可视化。

---

## 八、紧急联系 / 排错快查

### 业务报"打不开"

1. SSH 到 Lighthouse:`sudo systemctl status agent` 看服务是否在跑
2. 不在跑:`sudo journalctl -u agent -n 50` 看挂的原因
3. 通常解决:`sudo systemctl restart agent`

### 业务报"生成失败"

打开 F12 → Network → 看哪个 API 报错 + Response:
- 文案失败 → DeepSeek 余额没了 / key 过期
- 封面失败 → 豆包余额没了 / 模型 ID 变了
- 登录失败 → users.json 里没那个员工

### 域名打不开

```bash
dig mkt1.johnsonfitness.com.cn +short
# 必须返回 Lighthouse 公网 IP
```

如果返回的不是,检查 DNS 解析平台。

### 服务器满了

```bash
df -h           # 磁盘
free -h         # 内存
htop            # CPU 进程
```

通常重启服务释放内存即可:`sudo systemctl restart agent`。
