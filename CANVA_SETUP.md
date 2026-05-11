# Canva 模板配置(简化版)

零基础设施,无需 API、OAuth、付费数据库。**业务点一下 → 金句自动复制到剪贴板 → 新标签页打开 Canva 模板**。

---

## 一、维护模板列表

打开 `public/canva_templates.json`,这就是你要维护的唯一一个文件。

### 文件格式

```json
{
  "templates": [
    {
      "id": "zhongcao-1",
      "name": "种草测评 · 黄黑爆款",
      "category": "种草",
      "url": "https://www.canva.com/design/DAFxxxxx/yyy/edit?...",
      "desc": "适合产品测评、避雷类内容"
    }
  ]
}
```

字段说明:

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` | ✅ | 唯一标识,英文小写 + 连字符,如 `zhongcao-1` |
| `name` | ✅ | 网页上显示的标题 |
| `category` | 选 | 分类标签(显示在卡片左上角):种草 / 干货 / 促销 / 通用 等 |
| `url` | ✅ | Canva 模板的分享链接(见下面怎么拿) |
| `desc` | 选 | 一行小字描述 |

### 怎么拿 Canva 模板的分享链接

1. 在 Canva 里设计好你的封面模板
2. 右上角点 **Share / 分享**
3. 在分享菜单里找 **Template link** 或 **Use as Template**(不同账号类型可能位置略不同)
4. **Copy link / 复制链接**,这就是你要填进 `url` 字段的内容

> 如果你只是普通设计,没有 Template Link 选项,就用 **"Public view link"** 也行。但 Template Link 更好:别人打开会自动 fork 一份,不会改到你原版。

### 没有自己的模板?

`canva_templates.json` 默认配了 5 个**通用搜索链接**(浏览全部 / 种草 / 干货 / 促销 / 简洁),点了会跳到 Canva 模板库的对应分类搜索结果。先用这个跑通流程,然后逐步替换为你们自己设计的真实模板。

---

## 二、更新流程

每次改完 `canva_templates.json`,推到 GitHub 即可,Vercel 自动部署:

```bash
cd ~/Desktop/红书文案agent
git add public/canva_templates.json
git commit -m "更新 Canva 模板列表"
git push
```

约 1 分钟后,刷新网站就能看到新模板。

---

## 三、清理(可选)

如果你之前给"全自动 Canva 集成"配过这些环境变量,**现在都没用了,可以删**:

- `CANVA_CLIENT_ID`
- `CANVA_CLIENT_SECRET`
- `CANVA_REDIRECT_URI`
- `CANVA_REFRESH_TOKEN`
- `KV_REST_API_URL` / `KV_REST_API_TOKEN`(如果当时接过 KV)

不删也没问题,代码不会再读它们。

Canva Developer 后台你之前建的那个 integration 也可以删掉,或留着备用——它现在跑在"打开过 OAuth 但没用上"的状态,既不耗钱也不报错。

---

## 四、业务的最终使用流程

```
[网页]
选品牌 → 选产品 → 选文案类型 → 生成
       ↓
看到标题(5 条)/ 正文 / 标签
向下滚动看到"🎨 去 Canva 做封面"模块
       ↓
1. 点选一个金句(默认选第一个)
2. 点你贴好的模板分类(比如"种草测评")
       ↓
浏览器跳出 toast:✓ 金句已复制
新标签页:Canva 编辑器
       ↓
业务在 Canva 里:
  - 把金句粘到主标题文字框(Ctrl/Cmd + V)
  - 拖入自己拍的产品照,替换模板里的占位图
  - 调整字体大小、位置(可选)
       ↓
Share → Download → PNG → 完工
```

整个过程,业务方**只在 Canva 里操作 2 步**(粘贴文字 + 拖图替换),封面质量等于 Canva 模板本身的水平。
