"""代码版回复质检评分：渲染对话 → 调模型 → 解析校验。评分内核取自原项目。

用法：python code-version/score.py samples/conversations.json out/code.jsonl
环境变量：LLM_BASE_URL（OpenAI 兼容接口，含 /v1）、LLM_API_KEY、LLM_MODEL；
可选 LLM_FORMAT_MODE=schema|json|off，默认 schema，接口拒绝时自动降级，并在本次运行内记住
（DeepSeek 不支持 schema，可直接设成 json 省掉第一次被拒的请求）；
可选 LLM_EXTRA_BODY，一段 JSON，原样并进请求体（例如关闭思考模式）。
schema 里的长度上限不依赖接口执行：parse_score 把超长的 moment/reply/reply_zh 截到上限内最后一个句末，计入 clamped。
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT_VERSION = "score-v5"
PROMPT = (ROOT / "prompts" / (PROMPT_VERSION + ".txt")).read_text(encoding="utf-8")

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
SCORE_SCHEMA = {
    "type": "object",
    "required": ["scores", "total", "replies"],
    "properties": {
        "scores": {
            "type": "object",
            "required": list(DIMS),
            "properties": {dim: {"type": "integer", "minimum": 0, "maximum": 10} for dim in DIMS},
        },
        "total": {"type": "integer", "minimum": 0, "maximum": 100},
        "replies": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["idx", "verdict", "moment", "reply", "reply_zh"],
                "properties": {
                    "idx": {"type": "integer", "minimum": 1},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "moment": {"type": "string", "maxLength": 300},
                    "reply": {"type": "string", "maxLength": 180},
                    "reply_zh": {"type": "string", "maxLength": 400},
                },
            },
        },
    },
}

# 接口层的 maxLength 不一定生效（DeepSeek 只认 json_object，不认 JSON Schema），长度上限在解析时兜底
TEXT_LIMITS = {"moment": 300, "reply": 180, "reply_zh": 400}
# 句末：西文 .!? 后面是空白或文本结尾（"$2.95" 里的小数点不算），中文。！？直接算
SENTENCE_END = re.compile(r"[.!?](?=\s|$)|[。！？]")

FORMAT_MODES = ("schema", "json", "off")
MAX_RETRIES = 2
RETRY_INTERVAL = 1.0
# 本次运行里已验证可用的格式约束；某档被接口拒绝、下一档成功后，后面的请求直接从成功那档开始
_format_floor = 0


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


def render_segment(conv):
    def out(guard, detail="", context="", replies=0, masked=0):
        return {
            "guard": guard,
            "detail": detail,
            "context": context,
            "my_reply_count": replies,
            "masked_count": masked,
        }

    msgs = conv.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return out("invalid", "messages 必须是非空数组")

    lines = ["【客户】%s" % (conv.get("customer_name") or "").strip()]
    if conv.get("note"):
        lines.append("备注：%s" % conv["note"])
    if conv.get("bottom_lines"):
        lines.append("【公司报价底线】\n" + conv["bottom_lines"])
    if conv.get("knowledge"):
        lines.append("【公司知识库】\n" + conv["knowledge"])
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


def make_result(status, detail="", total=None, scores=None, replies=None, dropped=0, clamped=0):
    return {
        "status": status,
        "prompt_version": PROMPT_VERSION,
        "total": total,
        "scores": scores or {},
        "replies": replies or [],
        "dropped_replies": dropped,
        "clamped": clamped,
        "detail": detail,
    }


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


def parse_score(raw, my_reply_count):
    s = (raw or "").strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return make_result("unparseable", "模型输出里找不到 JSON 对象")
    try:
        obj = json.loads(s[i : j + 1])
    except ValueError as e:
        return make_result("unparseable", "JSON 解析失败：%s" % e)
    if not isinstance(obj, dict) or not isinstance(obj.get("scores"), dict):
        return make_result("unparseable", "缺少 scores 对象")

    scores, clamped = {}, 0
    for dim in DIMS:
        value, changed = clamp(obj["scores"].get(dim), 0, 10)
        if value is None:
            return make_result("unparseable", "维度 %s 不是整数" % dim)
        scores[dim] = value
        clamped += changed
    total, changed = clamp(obj.get("total"), 0, 100)
    if total is None:
        return make_result("unparseable", "total 不是整数")
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
    return make_result("ok", "", total, scores, replies, dropped, clamped)


def call_llm(messages, mode):
    body = {"model": os.environ["LLM_MODEL"], "messages": messages, "temperature": 0.4}
    body.update(json.loads(os.environ.get("LLM_EXTRA_BODY") or "{}"))
    if mode == "schema":
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "scorecard", "schema": SCORE_SCHEMA}}
    elif mode == "json":
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        os.environ["LLM_BASE_URL"].rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["LLM_API_KEY"]},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"].get("content") or "", data.get("usage") or {}


def score_context(context):
    """返回 (模型原文, 用量, 实际生效的格式约束)。格式被拒绝就降级；超时、限流和 5xx 重试，其余直接抛出。"""
    global _format_floor
    messages = [{"role": "system", "content": PROMPT}, {"role": "user", "content": context}]
    start = max(FORMAT_MODES.index(os.environ.get("LLM_FORMAT_MODE", "schema")), _format_floor)
    for mode in FORMAT_MODES[start:]:
        for attempt in range(MAX_RETRIES + 1):
            try:
                text, usage = call_llm(messages, mode)
                _format_floor = FORMAT_MODES.index(mode)
                return text, usage, mode
            except urllib.error.HTTPError as e:
                if e.code in (400, 422) and mode != "off":
                    break
                if attempt == MAX_RETRIES or (e.code < 500 and e.code != 429):
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == MAX_RETRIES:
                    raise
            time.sleep(RETRY_INTERVAL)


def score(conv):
    seg = render_segment(conv)
    record = {
        "id": conv.get("id"),
        "model": os.environ.get("LLM_MODEL"),
        "format_mode": None,
        "latency_ms": 0,
        "usage": {},
        "raw": None,  # 模型原文，用来核对校验阶段丢弃、钳制和截断了什么
    }
    if seg["guard"] != "ok":
        record["result"] = make_result("skipped" if seg["guard"] == "empty" else "invalid_input", seg["detail"])
        return record
    started = time.perf_counter()
    try:
        record["raw"], record["usage"], record["format_mode"] = score_context(seg["context"])
        record["result"] = parse_score(record["raw"], seg["my_reply_count"])
    except (urllib.error.URLError, TimeoutError, LookupError, ValueError) as e:
        record["result"] = make_result("llm_failed", "%s: %s" % (type(e).__name__, e))
    record["latency_ms"] = round((time.perf_counter() - started) * 1000)
    return record


def main(argv):
    if len(argv) != 3:
        sys.exit(__doc__)
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        if not os.environ.get(key):
            sys.exit("缺少环境变量 %s" % key)
    src, dst = Path(argv[1]), Path(argv[2])
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", encoding="utf-8", newline="\n") as out:
        for conv in json.loads(src.read_text(encoding="utf-8")):
            record = score(conv)
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(record["id"], record["result"]["status"], record["result"]["total"], "%dms" % record["latency_ms"])


if __name__ == "__main__":
    main(sys.argv)
