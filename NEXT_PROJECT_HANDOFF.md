# 下一个项目交接清单

> 用于开启**新对话框**时,快速把当前服务器状态告诉新的 AI 助手,避免破坏已有项目。

---

## 一、开新对话的开场白(可直接复制粘贴)

```
我要在一台已经部署好别的项目的腾讯云 Lighthouse 上,再部署 OpenClaw。
GitHub 仓库:https://github.com/openclaw/openclaw

【目标项目】
- OpenClaw 是 https://github.com/openclaw/openclaw 上的开源项目
  - 你帮我读一下 README,确认它是什么、怎么部署
  - 我要部署在云服务器上,通过子域名访问

【已有的云服务器(不能破坏现有服务)】
- 平台:腾讯云轻量应用服务器(Lighthouse)
- 配置:2 核 4GB,60GB SSD,5M 带宽,上海地域
- 系统:Ubuntu 22.04 LTS
- 公网 IP:124.222.164.101
- 登录方式:腾讯云控制台 WebSSH,默认用户 ubuntu,sudo 免密
- 端口已开放(防火墙 + 腾讯云控制台):22, 80, 443

【服务器现有状态(不要碰这些)】
- 已装:python3.10、pip、git、nginx、certbot、ufw
- 已运行:
  · systemd 服务 agent.service(占用端口 8000,小红书文案 agent)
  · nginx(配置在 /etc/nginx/sites-enabled/agent,代理 mkt1.johnsonfitness.com.cn → 8000)
- 已用域名:mkt1.johnsonfitness.com.cn(指向本机)
- 已有项目目录:/home/ubuntu/xiaohongshu-JohnsonAgent/

【OpenClaw 部署要求】
- 新分配端口(8001 起,避开 8000)
- 新 nginx server 配置(独立 server block)
- 新子域名:claw.johnsonfitness.com.cn(我之后会加 DNS A 记录指向 124.222.164.101)
- 新 HTTPS 证书(certbot 单独申请)
- systemd 服务托管,开机自启
- 不能影响 agent.service 和现有 nginx 配置

【我的能力水平】
- 会用 SSH WebSSH 终端,会粘贴命令
- 不熟 Docker / systemd / nginx 内部
- 出问题需要你给具体诊断命令,告诉我看什么

【希望你给我】
- 先看 README 告诉我 OpenClaw 是什么、部署需要 Docker 还是源码编译
- 然后给一份"从零部署 OpenClaw 到这台已有服务器"的傻瓜手册,每一步都是可复制粘贴的命令
- 部署中遇到的报错,逐条帮我排查
- 部署完成后写一份维护文档(类似 LIGHTHOUSE_DEPLOY.md 那种风格)
```

---

## 二、关键资源速查(以后任何新项目都用得上)

| 资源 | 值 |
|---|---|
| Lighthouse 公网 IP | `124.222.164.101` |
| Lighthouse 内网 IP | 进腾讯云控制台查 |
| 系统用户 | `ubuntu`(sudo 免密) |
| 服务器位置 | 上海 |
| 已占用端口 | 22(SSH)、80(HTTP)、443(HTTPS)、8000(小红书 agent) |
| 主域名 | `johnsonfitness.com.cn`(已备案) |
| 已用子域名 | `mkt1.johnsonfitness.com.cn` |
| Nginx 配置目录 | `/etc/nginx/sites-available/` + `sites-enabled/` |
| systemd 服务目录 | `/etc/systemd/system/` |
| 项目代码目录约定 | `/home/ubuntu/<项目名>/` |

---

## 三、加新项目时的"四件套"流程

任何新项目部署到这台服务器,都遵循这个范式:

1. **新端口**(8001、8002、8003 …)— 避开 8000
2. **新子域名**(claw、tool、ai 等).johnsonfitness.com.cn — DNS 加 A 记录指向 124.222.164.101
3. **新 nginx server block** — `/etc/nginx/sites-available/<项目名>`,反代到对应端口
4. **新 systemd service** — `/etc/systemd/system/<项目名>.service`,守护进程

```
[端口分配建议]
8000  ← 小红书文案 agent(已占)
8001  ← OpenClaw(待部署)
8002  ← 留给下个项目
8003  ← 留给下个项目
...
```

---

## 四、新增子域名的标准流程(5 分钟)

每次开新项目,**DNS 这一步都一样**:

1. 进你公司域名 DNS 后台(DNSPod / 阿里云 / 华为云)
2. 加 A 记录:
   - 主机记录:`claw`(或别的子域名前缀)
   - 记录类型:`A`
   - 记录值:`124.222.164.101`
   - TTL:`600`
3. 等 5-10 分钟生效
4. 在 Lighthouse 上跑 `certbot --nginx -d claw.johnsonfitness.com.cn` 申请证书

子域名是无限的、免费的,且自动继承主域名备案。

---

## 五、跨项目共享的踩坑经验(必看)

新部署任何项目时,这些坑都会再遇到:

1. **apt install 偶尔静默失败** — 每装一个包都 `--version` 验证
2. **GitHub clone TLS 中断** — 用 `--depth 1` + ghproxy 镜像
3. **Lighthouse 控制台防火墙在实例详情页**,不在列表页
4. **`ufw enable` 前必须 `allow 22`**,否则切断 SSH
5. **systemd EnvironmentFile 路径必须用绝对路径**
6. **域名 DNS 改完要等 5-10 分钟生效**,certbot 才能验证成功
7. **Python 源码涉及中文优先用单引号包字符串**,避免英文双引号冲突

完整 12 个踩坑详见 `LIGHTHOUSE_DEPLOY.md` 第四章。

---

## 六、紧急情况下停所有服务

```bash
sudo systemctl stop agent          # 停小红书 agent
sudo systemctl stop <openclaw>     # 停 OpenClaw(部署后这个名字)
sudo systemctl stop nginx          # 停 nginx
```

恢复:把 stop 换成 start。
