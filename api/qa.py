"""POST /api/qa
基于 manuals 表的严格 grounding 售前问答 API。

请求体:
    {
        "brand": "乔山Johnson",
        "product": "智能跑步机TX-5",
        "question": "跑带宽是多少",
        "_user": {...}    // 自动注入
    }

响应:
    {
        "answer": "据 [说明书 第5页]:跑带宽 50cm...",
        "sources": [{"type": "...", "file": "...", "page": 5}, ...],
        "context_chars": 12345,
        "no_data": false,
        "model": "deepseek-chat"
    }

防幻觉三层:
    1) 智能选段(优先卖点 docx + 单页,说明书按关键词打分)
    2) 强 prompt + temperature 0.1
    3) 返回中带页码,业务可验
"""
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import os
import re
import ssl
import sys
import json
import sqlite3
import urllib.request
import urllib.error


def _build_ssl_context():
    """Mac Python 默认证书空缺时,用 certifi。装了 certifi 用它,没装就系统默认。"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL_CTX = _build_ssl_context()


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / 'data' / 'usage.db'

DEEPSEEK_URL = 'https://api.deepseek.com/chat/completions'

# 上下文预算:DeepSeek 32k tokens ≈ 6w-9w 中文字符。
# 保守留 4000 tokens 给输出 → 输入约 28k tokens ≈ 40k 中文字符。
# 再保守一点取 24000 字以稳定。
MAX_CONTEXT_CHARS = 24000
# 单产品手册最多塞多少页(按打分排序后取前 K)
MAX_MANUAL_PAGES = 12

HOTLINE = "全国售后服务热线：4000480500，周一至周五上午 8:30-17:30"
WARRANTY_FALLBACK = "整机质保一年，主体框架质保十年"

# 触发售后/保修兜底的关键词(主回答里出现这些就要追加热线)
AFTER_SALES_KEYWORDS = (
    '售后', '保修', '质保', '维修', '故障', '客服', '退换',
    '保养', '配件', '热线', '咨询电话',
)

SYSTEM_PROMPT = f"""你是乔山健身的产品售前问答助手,服务于一线业务员、经销商、客服。
你的回答会直接被业务员复制粘贴发给客户,务必准确、规范、有说服力。

【绝对规则】
1. 只能基于下方"可用资料"回答,严禁编造任何参数、数字、功能、安全提示、保养步骤。
2. 如果资料里没有相关信息,必须明确说「资料里没有这方面的内容,建议查阅完整说明书或联系产品经理」,不要硬猜或推理。
3. 数字、单位、规格,严格照原文,不要四舍五入,不要补全。
4. 绝对禁止使用 markdown(**加粗**、# 标题、>引用),输出纯文本即可。
5. **不要**写「依据:xxx」、「根据资料显示」、「基于上下文」之类的话,直接给答案。

【特殊兜底规则】
A. 保修期相关问题(用户问到 保修 / 质保 / 维修期):
   - 若资料里有明确写,优先用资料原文。
   - 若资料里没写,使用默认答案:「{WARRANTY_FALLBACK}」。
   - 无论哪种情况,都在结尾加一行客服热线。
B. 售后/维修/保养/配件 相关问题:
   - 在答案末尾追加一行客服热线(避免业务漏报):
     {HOTLINE}
C. 客服热线统一固定为:{HOTLINE}

【排版要求】(让答案"扫一眼就看明白",业务直接发给客户也体面)
- 「卖点 / 核心参数 / 安全注意事项 / 保养步骤 / 故障排查」这类**可枚举内容**,必须用「1. 2. 3.」编号,每条占一行。
- 不同语义块之间空一行,排版要透气。
- 编号 + 空格 + 内容,例:「1. 跑带宽 50cm」,不要用 • 或 - 或 *。
- 段落开头不要"以下是" / "下面我整理了"这种过渡词,直接进内容。

【风格】
- 口语化但不轻浮,像一个懂产品的资深客服。
- 简洁,不啰嗦,不重复客户问题。

【输出结构】(每次回答必须严格遵守这个两段式)

主回答内容(按上述规则)

(空一行)
销售建议：
1-3 句帮一线业务推进成交的话术,要紧扣本次客户的具体问题,基于资料里的事实自然引出引导成交角度。
不要再罗列产品参数,不要硬卖,不要重复客户提问。例:
- 问尺寸 → 建议确认家里可摆放面积(给出可参考的运动空间数值);若产品可折叠,提示这点
- 问参数/对比 → 引导到适合人群和典型使用场景
- 问保养 → 强调维护简单、配件易得、降低购买顾虑
- 问保修 → 强调质保政策、品牌信任、客服热线易触达

输出示例(供你参考结构,内容请按真实资料):
══════════════════════════════
TX-5 主要尺寸:
1. 整机尺寸 195×85×140cm
2. 跑带尺寸 145×50cm,适合身高 150-195cm 用户
3. 折叠后占地约 0.8㎡

销售建议：
1. 可以先了解下客户家里给跑步机预留的位置大概多大,通常建议预留 2.5㎡ 的运动空间会比较舒服。
2. TX-5 支持轻松折叠,家里空间紧张也不用担心,平时不用收起来很方便。
══════════════════════════════
"""


# ──────────────────────── 检索:智能选段 ────────────────────────

# 简单中文停用词,避免噪声 ngram
_STOP_WORDS = {
    '你好', '请问', '怎么', '怎样', '如何', '多少', '什么', '哪些', '哪个', '是吗',
    '能不能', '可不可以', '有没有', '一下', '可以', '需要', '请教', '介绍',
    '问下', '咨询', '说一下', '帮我',
}


def chinese_ngrams(text, sizes=(2, 3, 4)):
    """从一段文本里抽中文 ngram 关键词 + 完整英数词。
    支持中英混排:把混合段落拆成连续中文段 + 连续英数段,各自处理。
    """
    clean = re.sub(r'[^一-龥a-zA-Z0-9]+', ' ', text)
    grams = set()
    for seg in clean.split():
        if not seg or seg in _STOP_WORDS:
            continue
        # 把混排串拆成「连续中文」和「连续英数」的子段
        subs = re.findall(r'[一-龥]+|[a-zA-Z0-9]+', seg)
        for sub in subs:
            if len(sub) < 2 or sub in _STOP_WORDS:
                continue
            grams.add(sub)
            # 中文子段做 ngram
            if '一' <= sub[0] <= '龥' and len(sub) >= 2:
                for n in sizes:
                    for i in range(len(sub) - n + 1):
                        g = sub[i:i + n]
                        if g not in _STOP_WORDS:
                            grams.add(g)
    return grams


def query_product_chunks(brand, product, question):
    """从 manuals 表拉该产品的所有内容,智能装入上下文。
    返回 (context_string, used_chars, sources_list)
    """
    if not DB_PATH.exists():
        return None, 0, []

    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT source_type, source_file, page_no, content "
        "FROM manuals WHERE brand=? AND product=? "
        "ORDER BY source_type, page_no",
        (brand, product),
    ).fetchall()
    conn.close()

    if not rows:
        return None, 0, []

    selling, onepager, manual = [], [], []
    for st, sf, pg, ct in rows:
        item = {'source_type': st, 'source_file': sf, 'page_no': pg, 'content': ct}
        if st == 'selling_docx':
            selling.append(item)
        elif st == 'onepager_pdf':
            onepager.append(item)
        elif st == 'manual_pdf':
            manual.append(item)
        else:
            # other_pdf 也归到 manual 一起处理
            manual.append(item)

    chunks = []
    sources = []
    used = 0

    def try_add(item, tag_label):
        nonlocal used
        text = (item['content'] or '').strip()
        if not text or text == '[无文字内容]':
            return False
        header = f"[{tag_label} | {item['source_file']} 第{item['page_no']}页]"
        chunk = f"{header}\n{text}"
        # +2 是后面 join 的 \n\n
        if used + len(chunk) + 2 > MAX_CONTEXT_CHARS:
            return False
        chunks.append(chunk)
        sources.append({
            'type': tag_label,
            'file': item['source_file'],
            'page': item['page_no'],
        })
        used += len(chunk) + 2
        return True

    # Pass 1:卖点 docx 全收(总字数小,价值最高)
    for s in selling:
        try_add(s, '卖点整理')
    # Pass 2:单页全收(规格集中)
    for o in onepager:
        try_add(o, '产品单页')

    # Pass 3:说明书按关键词打分 + 前 5 页基础分
    if manual:
        kws = chinese_ngrams(question)
        scored = []
        for m in manual:
            score = 0
            content = m['content']
            for k in kws:
                # 单个 ngram 出现一次得 1 分,长 ngram 得分更高(更具体)
                cnt = content.count(k)
                if cnt > 0:
                    score += cnt * (1 + 0.2 * (len(k) - 2))
            # 前 5 页给基础分(封面/目录/安全须知,几乎所有问题都可能需要)
            if m['page_no'] <= 5:
                score += 2
            scored.append((score, m))
        scored.sort(key=lambda x: (-x[0], x[1]['page_no']))
        added_pages = 0
        for _, m in scored:
            if added_pages >= MAX_MANUAL_PAGES:
                break
            if try_add(m, '说明书'):
                added_pages += 1
            else:
                break

    return '\n\n'.join(chunks), used, sources


# ──────────────────────── DeepSeek 调用 ────────────────────────

def call_deepseek(system_prompt, user_prompt):
    api_key = os.environ.get('DEEPSEEK_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY 未配置")

    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 1500,
    }
    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return data['choices'][0]['message']['content']
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='ignore') if e.fp else ''
        raise RuntimeError(f"DeepSeek API 错误 {e.code}: {detail[:300]}")


# ──────────────────────── 后处理 ────────────────────────

def clean_answer(answer):
    """清掉模型偶发输出的 markdown 标记"""
    if not isinstance(answer, str):
        return answer
    s = (answer.replace("\\r\\n", "\n")
                .replace("\\n", "\n")
                .replace("\\r", "\n")
                .replace("\\t", "  "))
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"__(.+?)__", r"\1", s)
    s = re.sub(r"(?m)^[ \t]*#{1,6}[ \t]*", "", s)
    s = re.sub(r"(?m)^[ \t]*>[ \t]?", "", s)
    # 注意:bullets 改写成 1. 2. 3. 是 prompt 的事,这里只兜底去掉 markdown - * +
    s = re.sub(r"(?m)^[ \t]*[-*+][ \t]+", "", s)
    s = s.replace("**", "")
    # 兜底去掉模型偷偷写的「依据:xxx」/「依据：xxx」一行
    s = re.sub(r"(?m)^[ \t]*依据[：:].*$", "", s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def enforce_hotline_and_warranty(answer, question):
    """后处理兜底:
    - 售后/保修/维修类话题,确保答案里出现客服热线
    - 用户问「保修期」但答案里既没有 整机质保 也没有 资料没有 的措辞 → 在主回答处追加默认质保
    """
    if not isinstance(answer, str):
        return answer

    q = question or ''
    a = answer

    # 步骤 1:保修兜底优先(它会自动追加默认质保 + 热线)
    warranty_keywords = ('保修', '质保', '维保', '保几年', '保多久')
    if any(k in q for k in warranty_keywords):
        has_fallback = '整机质保' in a or '质保十年' in a
        looks_missing = any(p in a for p in ('资料里没有', '未提及', '没有这方面', '资料中未', '没有相关'))
        if looks_missing and not has_fallback:
            insert = f'\n根据乔山品牌统一售后政策:{WARRANTY_FALLBACK}。'
            if '销售建议' in a:
                a = re.sub(r'(\n*销售建议[：:])', f'\n{insert}\n\\1', a, count=1)
            else:
                a = a.rstrip() + '\n' + insert

    # 步骤 2:售后/保修类话题确保热线出现
    triggers_q = any(k in q for k in AFTER_SALES_KEYWORDS)
    triggers_a = any(k in a for k in AFTER_SALES_KEYWORDS)
    if (triggers_q or triggers_a) and HOTLINE not in a:
        # 销售建议之前插
        if '销售建议' in a:
            a = re.sub(r'(\n*销售建议[：:])', f'\n\n{HOTLINE}\n\n\\1', a, count=1)
        else:
            a = a.rstrip() + f'\n\n{HOTLINE}'

    # 折叠 3+ 个连续 \n 为 2 个,避免视觉空白过多
    a = re.sub(r'\n{3,}', '\n\n', a)
    return a.strip()


# ──────────────────────── 现有产品列表 ────────────────────────

def list_available_products():
    """从 manuals 表里查出有数据的产品,供前端下拉用"""
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        "SELECT brand, product, COUNT(*) chunks, SUM(char_count) chars "
        "FROM manuals GROUP BY brand, product "
        "ORDER BY brand, product"
    ).fetchall()
    conn.close()
    out = {}
    for brand, product, chunks, chars in rows:
        out.setdefault(brand, []).append({
            'name': product,
            'chunks': chunks,
            'chars': chars,
        })
    return out


# ──────────────────────── HTTP handler ────────────────────────

class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        """GET /api/qa?action=products 返回当前可问答的产品列表"""
        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            action = (qs.get('action', [''])[0]).strip()
            if action == 'products':
                return self._json(200, {'products': list_available_products()})
            return self._error(400, "未知 action")
        except Exception as e:
            self._error(500, str(e))

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            req = json.loads(body)

            brand = (req.get("brand") or "").strip()
            product = (req.get("product") or "").strip()
            question = (req.get("question") or "").strip()
            from lib.session import user_from_headers
            user = user_from_headers(self.headers)
            if not user:
                return self._error(401, "未登录或登录已过期,请重新登录")
            from lib.ratelimit import check as _rl_check
            _ok, _msg = _rl_check(user, self.client_address[0] if self.client_address else '', 'qa')
            if not _ok:
                return self._error(429, _msg)

            print(
                f"[USAGE] action=qa user={user.get('emp_id')}/{user.get('name')}/{user.get('department')} "
                f"brand={brand} product={product} q={question[:60]}",
                flush=True,
            )

            if not all([brand, product, question]):
                return self._error(400, "请填写品牌、产品和问题")
            if len(question) > 500:
                return self._error(400, "问题太长,请精简到 500 字以内")

            context, used_chars, sources = query_product_chunks(brand, product, question)

            # 没数据的兜底
            if not context:
                self._json(200, {
                    'answer': f'抱歉,资料库里还没有「{product}」的内容。请联系管理员补充。',
                    'sources': [],
                    'context_chars': 0,
                    'no_data': True,
                    'model': None,
                })
                self._log_qa(user, brand, product, question, used_chars=0, no_data=True)
                return

            user_prompt = (
                f"【产品】{brand} / {product}\n\n"
                f"【可用资料】(以下是这个产品的全部可用资料,你只能基于这些回答)\n\n"
                f"{context}\n\n"
                f"【提问】{question}\n\n"
                f"请基于上方资料给出准确、口语化的回答。如果资料里没有,明确说没有,不要推理。"
                f"答案末尾用一行附「依据:xxx」。"
            )

            answer = call_deepseek(SYSTEM_PROMPT, user_prompt)
            answer = clean_answer(answer)
            answer = enforce_hotline_and_warranty(answer, question)

            self._log_qa(user, brand, product, question, used_chars=used_chars, no_data=False)

            self._json(200, {
                'answer': answer,
                'sources': sources,
                'context_chars': used_chars,
                'no_data': False,
                'model': 'deepseek-chat',
            })
        except Exception as e:
            self._log_qa_failure(req if 'req' in dir() else {}, str(e))
            self._error(500, str(e))

    # ── 日志写入(失败静默) ──
    def _log_qa(self, user, brand, product, question, used_chars, no_data):
        try:
            sys.path.insert(0, str(ROOT))
            from lib.kv_store import log_event
            log_event('qa', user or {}, {
                'brand': brand,
                'product': product,
                'question': question[:200],
                'context_chars': used_chars,
                'no_data': no_data,
            })
        except Exception:
            pass

    def _log_qa_failure(self, req, reason):
        try:
            sys.path.insert(0, str(ROOT))
            from lib.kv_store import log_event
            user = (req or {}).get('_user') if isinstance(req, dict) else {}
            log_event('qa_failed', user or {}, {
                'brand': (req or {}).get('brand'),
                'product': (req or {}).get('product'),
                'reason': str(reason)[:300],
            })
        except Exception:
            pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, msg):
        self._json(code, {"error": msg})
