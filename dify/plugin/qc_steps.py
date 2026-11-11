"""回复质检里不调模型的三个步骤：脱敏、渲染、校验。逻辑与 Dify 代码节点、code-version/score.py 相同，
由 tests/test_parity.py 对照。"""

import json
import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
CARD_RE = re.compile(r"\b(?:\d{4}[ -]){2,4}\d{4}\b|\b\d{13,19}\b")
PHONE_RE = re.compile(
    r"(?<![A-Za-z0-9])\+?\d{1,3}[ .\-]?(?:\(\d{1,4}\)[ .\-]?)?\d{2,4}(?:[ .\-]?\d{2,4}){1,4}(?![A-Za-z0-9])"
)
DATE_RE = re.compile(r"\d{4}[-/.]\d{1,2}[-/.]\d{1,2}")
RULES = [("BANKCARD", CARD_RE), ("PHONE", PHONE_RE), ("EMAIL", EMAIL_RE)]
CONTEXT_LIMIT = 6500
MARKER = "【对话记录】"

DIMS = ("responsiveness", "discovery", "negotiation", "risk_handling", "professionalism")
VERDICTS = ("good", "ok", "bad")
# 评分节点用 json_object（DeepSeek 不认 JSON Schema），长度上限在这里兜底
TEXT_LIMITS = {"moment": 300, "reply": 180, "reply_zh": 400}
# 句末：西文 .!? 后面是空白或文本结尾（"$2.95" 里的小数点不算），中文。！？直接算
SENTENCE_END = re.compile(r"[.!?](?=\s|$)|[。！？]")


def is_phone(s):
    # 数量（10000 pcs）和日期也会命中手机号正则，按位数、前缀和分隔符排除
    digits = sum(ch.isdigit() for ch in s)
    if digits < 7 or digits > 15 or DATE_RE.match(s):
        return False
    return s.startswith("+") or digits >= 10 or not s.isdigit()


def mask_text(text):
    order = [kind for kind, _ in RULES]
    cands = sorted(
        ((m.start(), m.end(), kind) for kind, rx in RULES for m in rx.finditer(text)),
        key=lambda c: (c[0], order.index(c[2])),
    )
    out, pos, counters = [], 0, {}
    for start, end, kind in cands:
        if start < pos or (kind == "PHONE" and not is_phone(text[start:end])):
            continue
        counters[kind] = counters.get(kind, 0) + 1
        out.append(text[pos:start])
        out.append("[%s_%d]" % (kind, counters[kind]))
        pos = end
    out.append(text[pos:])
    return "".join(out), sum(counters.values())


def render(customer_name, note, messages, bottom_lines, knowledge):
    def out(guard, detail="", context="", replies=0, masked=0):
        return {
            "guard": guard,
            "detail": detail,
            "context": context,
            "my_reply_count": replies,
            "masked_count": masked,
        }

    try:
        msgs = json.loads(messages or "")
    except ValueError as e:
        return out("invalid", "messages 不是合法 JSON：%s" % e)
    if not isinstance(msgs, list) or not msgs:
        return out("invalid", "messages 必须是非空数组")

    lines = ["【客户】%s" % (customer_name or "").strip()]
    if note:
        lines.append("备注：%s" % note)
    if bottom_lines:
        lines.append("【公司报价底线】\n" + bottom_lines)
    if knowledge:
        lines.append("【公司知识库】\n" + knowledge)
    lines.append(MARKER)

    replies = masked = 0
    for i, m in enumerate(msgs):
        if not isinstance(m, dict) or not isinstance(m.get("text"), str) or not isinstance(m.get("from_me"), bool):
            return out("invalid", "第 %d 条消息需要 text（字符串）和 from_me（布尔）" % (i + 1))
        text, hits = mask_text(m["text"])
        masked += hits
        if m["from_me"]:
            replies += 1
            who = "我方#%d" % replies
        else:
            who = "客户"
        lines.append("%s 【%s】%s" % (m.get("ts") or "", who, text))
    if replies == 0:
        return out("empty", "没有我方回复，不调用模型")

    block = "\n".join(lines)
    if len(block) > CONTEXT_LIMIT:
        # 只截对话记录，头部的底线和知识库完整保留；保留最近的消息
        cut = block.find(MARKER) + len(MARKER)
        block = block[:cut] + block[cut:][-(CONTEXT_LIMIT - cut):]
    return out("ok", "", block, replies, masked)


def clamp(value, lo, hi):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None, 0
    c = max(lo, min(hi, n))
    return c, int(c != n)


def fit(text, limit):
    """超长文本截到上限内最后一个句末；没有句末截到最后一个空格，再没有就硬截。
    返回 (文本, 保留的整句数)：没超长为 None，句中截断为 0。"""
    if len(text) <= limit:
        return text, None
    ends = [m.end() for m in SENTENCE_END.finditer(text) if m.end() <= limit]
    if ends:
        return text[: ends[-1]].rstrip(), len(ends)
    head = text[:limit]
    space = head.rfind(" ")
    return (head[:space] if space > 0 else head).rstrip(), 0


def keep_sentences(text, k):
    """只保留前 k 句；不足 k 句时原样返回。"""
    ends = [m.end() for m in SENTENCE_END.finditer(text)]
    if 0 < k <= len(ends) and text[ends[k - 1] :].strip():
        return text[: ends[k - 1]].rstrip()
    return text


def validate(raw, my_reply_count, prompt_version):
    def result(status, detail="", total=None, scores=None, replies=None, dropped=0, clamped=0):
        return {
            "status": status,
            "prompt_version": prompt_version,
            "total": total,
            "scores": scores or {},
            "replies": replies or [],
            "dropped_replies": dropped,
            "clamped": clamped,
            "detail": detail,
        }

    s = (raw or "").strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return result("unparseable", "模型输出里找不到 JSON 对象")
    try:
        obj = json.loads(s[i : j + 1])
    except ValueError as e:
        return result("unparseable", "JSON 解析失败：%s" % e)
    if not isinstance(obj, dict) or not isinstance(obj.get("scores"), dict):
        return result("unparseable", "缺少 scores 对象")

    scores, clamped = {}, 0
    for dim in DIMS:
        value, changed = clamp(obj["scores"].get(dim), 0, 10)
        if value is None:
            return result("unparseable", "维度 %s 不是整数" % dim)
        scores[dim] = value
        clamped += changed
    total, changed = clamp(obj.get("total"), 0, 100)
    if total is None:
        return result("unparseable", "total 不是整数")
    clamped += changed

    replies, dropped = [], 0
    for r in obj.get("replies") or []:
        if not isinstance(r, dict):
            dropped += 1
            continue
        try:
            idx = int(r.get("idx"))
        except (TypeError, ValueError):
            dropped += 1
            continue
        verdict = str(r.get("verdict") or "").strip()
        # 模型多造的编号（超过我方回复条数）和非法评级一律丢弃，并计数
        if idx < 1 or idx > int(my_reply_count or 0) or verdict not in VERDICTS:
            dropped += 1
            continue
        reply = str(r.get("reply") or "").strip()
        if verdict == "bad" and not reply:
            verdict = "ok"
        moment, moment_cut = fit(str(r.get("moment") or "").strip(), TEXT_LIMITS["moment"])
        reply, reply_cut = fit(reply, TEXT_LIMITS["reply"])
        reply_zh = str(r.get("reply_zh") or "").strip()
        if reply_cut:
            # reply 按整句截短了，译文跟着保留同样的句数
            reply_zh = keep_sentences(reply_zh, reply_cut)
        reply_zh, zh_cut = fit(reply_zh, TEXT_LIMITS["reply_zh"])
        clamped += sum(cut is not None for cut in (moment_cut, reply_cut, zh_cut))
        replies.append({"idx": idx, "verdict": verdict, "moment": moment, "reply": reply, "reply_zh": reply_zh})
    return result("ok", "", total, scores, replies, dropped, clamped)
