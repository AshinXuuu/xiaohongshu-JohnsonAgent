# 产品资料库 · 上线配置手册

> 给「产品资料库」下载功能接上腾讯云对象存储(COS)。
> 全程在腾讯云控制台 + 服务器 WebSSH 操作,按顺序复制粘贴即可。

---

## 功能说明

业务在工作台点「产品资料库」→ 按品牌 / 产品找到资料 → 点「下载」。
后端用你的 COS 密钥**临时签发一条 5 分钟有效的下载链接**,桶始终保持私有,链接外泄也会过期。
价格表等「价格与政策」资料**不进下载范围**,且就算有人猜 key 也下不到(后端有白名单)。

---

## 一、桶设为「私有读写」(重要)

1. 进 [COS 控制台](https://console.cloud.tencent.com/cos/bucket) → 选你的桶
2. 「权限管理 → 访问权限」确认是 **私有读写**(默认就是,别改成公有读)

> 私有 = 别人直接拿桶里文件 URL 打不开,只有我们后端签发的临时链接能下。

---

## 二、确认产品库已上传 + 记住「前缀」

资料下载靠一份清单 `data/library_manifest.json`(已在代码里,24 款产品 / 66 份 PDF)。
清单里的路径形如 `乔山Johnson/专业系列动感单车GR7/GR7单页_画板 1.pdf`。

COS 上的实际对象 Key = **前缀 COS_PREFIX + 清单路径**,所以前缀要和你上传方式对上:

| 你上传的方式 | COS_PREFIX 填 |
|---|---|
| 把整个「产品库」文件夹拖进桶 | `产品库/` |
| 只把产品库**里面的内容**传进桶根目录 | 留空 |
| 传进某个子目录,如 `materials/产品库/` | `materials/产品库/` |

> 不确定就先按上传时的样子填,第六步有脚本会帮你验证对不对。

---

## 三、创建一个「只读」子账号密钥(别用主账号密钥)

1. 进 [访问管理 CAM → 用户 → 子用户](https://console.cloud.tencent.com/cam) → 新建子用户(编程访问)
2. 给它授权 `QcloudCOSDataReadOnly`(只读)+ 限定到你这个桶最稳妥
3. 记下它的 **SecretId** 和 **SecretKey**(只读权限,万一泄露风险也小)

---

## 四、服务器装依赖

```bash
cd /home/ubuntu/xiaohongshu-JohnsonAgent
pip3 install cos-python-sdk-v5 --break-system-packages
python3 -c "import qcloud_cos; print('cos sdk ok')"
```

---

## 五、配 .env

编辑服务器上的 `.env`,把下面几行加进去(值换成你自己的):

```bash
COS_SECRET_ID=AKIDxxxxxxxx
COS_SECRET_KEY=xxxxxxxx
COS_REGION=ap-shanghai          # 桶详情页能看到地域,如 ap-shanghai / ap-guangzhou
COS_BUCKET=johnson-1250000000   # 桶名,务必带后面的 APPID 数字
COS_PREFIX=产品库/               # 见第二步
COS_URL_EXPIRE=300
```

---

## 六、拉代码 + 验证 key 对不对 + 重启

```bash
cd /home/ubuntu/xiaohongshu-JohnsonAgent
git pull

# 载入 .env 里的变量,跑校验脚本(确认前缀/文件都对得上)
set -a && . ./.env && set +a
python3 scripts/verify_cos.py
```

- 看到「全部命中 ✅」→ 继续重启
- 看到「缺失 N 个」→ 多半是 COS_PREFIX 没对上,按脚本提示调整 `.env` 里的 COS_PREFIX 再跑一次

```bash
sudo systemctl restart agent
```

---

## 七、验收

手机或电脑打开工作台 → 登录 → 「产品资料库」→ 随便点一个产品的「下载」,
能弹出 PDF 下载就成功了。

---

## 日常维护

- **产品库有增减**:更新本地「产品库」→ 重新上传 COS → 本地 `python3 scripts/gen_library_manifest.py` 重生成清单 → push → 服务器 `git pull` + 重启。
- **某个文件分类错了**(比如把中文说明书标成了英文):直接改 `data/library_manifest.json` 里那条的 `"type"` 字段(可选值:单页 / 中文说明书 / 英文说明书)→ push → 服务器 `git pull` + 重启。
- **想看谁下了什么**:下载动作已记进用量库(action=`download`),后续可在管理后台加一个维度统计。

---

## 安全小结

- 桶私有,链接 5 分钟过期
- 子账号只读密钥,不碰主账号
- 后端白名单:只有清单内的文件能下,「价格与政策」永远下不到
- 需登录才能进资料库页(沿用现有 24h 会话)
