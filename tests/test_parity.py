"""Deterministic steps must behave the same in the code version and in the Dify code nodes."""

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
DSL = ROOT / "dify" / "reply-qc-score.yml"
VERSION = "score-v5"


def _code_impl():
    spec = importlib.util.spec_from_file_location("score", ROOT / "code-version" / "score.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {
        "mask": mod.mask_text,
        "render": mod.render_segment,
        "validate": lambda raw, n: mod.parse_score(raw, n),
    }


def _dify_node(node_id):
    graph = yaml.safe_load(DSL.read_text(encoding="utf-8"))["workflow"]["graph"]
    data = next(n["data"] for n in graph["nodes"] if n["id"] == node_id)
    scope = {}
    exec(data["code"], scope)
    return scope


def _dify_impl():
    render, validate = _dify_node("n_render"), _dify_node("n_validate")

    def run_render(conv):
        return render["main"](
            conv.get("customer_name"),
            conv.get("note"),
            json.dumps(conv["messages"], ensure_ascii=False) if "messages" in conv else conv.get("raw_messages"),
            conv.get("bottom_lines"),
            conv.get("knowledge"),
        )

    return {
        "mask": render["mask_text"],
        "render": run_render,
        "validate": lambda raw, n: validate["main"](raw, n, VERSION)["result"],
    }


@pytest.fixture(params=["code", "dify"])
def impl(request):
    return _code_impl() if request.param == "code" else _dify_impl()


def msg(from_me, text, ts="11-03 09:00"):
    return {"ts": ts, "from_me": from_me, "text": text}


def card(scores=None, total=70, replies=None):
    base = {"responsiveness": 7, "discovery": 6, "negotiation": 5, "risk_handling": 5, "professionalism": 8}
    base.update(scores or {})
    return json.dumps({"scores": base, "total": total, "replies": replies or []}, ensure_ascii=False)


@pytest.mark.parametrize(
    "text, expected, count",
    [
        ("call +34 612 345 678 now", "call [PHONE_1] now", 1),
        ("wechat 13800138000", "wechat [PHONE_1]", 1),
        ("mail carlos.demo@example.com", "mail [EMAIL_1]", 1),
        ("card 4111 1111 1111 1111 ok", "card [BANKCARD_1] ok", 1),
        ("I want 10000 pcs", "I want 10000 pcs", 0),
        ("1000000 pcs per year", "1000000 pcs per year", 0),
        ("ship on 2026-11-18 09:30", "ship on 2026-11-18 09:30", 0),
        ("price $2.95 for 3000 pcs", "price $2.95 for 3000 pcs", 0),
    ],
)
def test_mask(impl, text, expected, count):
    assert impl["mask"](text) == (expected, count)


def test_render_numbers_our_replies_and_masks(impl):
    out = impl["render"](
        {
            "customer_name": "Carlos",
            "bottom_lines": "底线 $2.70",
            "messages": [
                msg(False, "call me +34 612 345 678", "11-05 15:00"),
                msg(True, "ok", "11-05 15:04"),
                msg(False, "10000 pcs"),
                msg(True, "noted"),
            ],
        }
    )
    assert out["guard"] == "ok"
    assert out["my_reply_count"] == 2
    assert out["masked_count"] == 1
    assert out["context"].splitlines() == [
        "【客户】Carlos",
        "【公司报价底线】",
        "底线 $2.70",
        "【对话记录】",
        "11-05 15:00 【客户】call me [PHONE_1]",
        "11-05 15:04 【我方#1】ok",
        "11-03 09:00 【客户】10000 pcs",
        "11-03 09:00 【我方#2】noted",
    ]


def test_render_without_our_replies_is_empty(impl):
    out = impl["render"]({"customer_name": "Ivan", "messages": [msg(False, "Hello?")]})
    assert (out["guard"], out["my_reply_count"], out["context"]) == ("empty", 0, "")
    assert out["detail"]


@pytest.mark.parametrize(
    "conv",
    [
        {"customer_name": "x", "raw_messages": "not json"},
        {"customer_name": "x", "raw_messages": None},
        {"customer_name": "x", "raw_messages": "{}"},
        {"customer_name": "x", "messages": []},
        {"customer_name": "x", "messages": [{"from_me": True}]},
        {"customer_name": "x", "messages": [{"from_me": "false", "text": "hi"}]},
    ],
)
def test_render_rejects_bad_input(impl, conv):
    out = impl["render"](conv)
    assert out["guard"] == "invalid"
    assert out["detail"]
    assert out["context"] == ""


def test_render_truncates_only_the_conversation(impl):
    msgs = [msg(False, "old " + "x" * 400) for _ in range(30)] + [msg(True, "LAST REPLY")]
    out = impl["render"]({"customer_name": "Long", "bottom_lines": "底线 $2.70", "messages": msgs})
    assert out["guard"] == "ok"
    assert len(out["context"]) == 6500
    assert out["context"].startswith("【客户】Long\n【公司报价底线】\n底线 $2.70\n【对话记录】")
    assert out["context"].endswith("【我方#1】LAST REPLY")


def test_validate_ok_passthrough(impl):
    replies = [
        {"idx": 1, "verdict": "bad", "moment": "m", "reply": "Which port?", "reply_zh": "哪个港口？"},
        {"idx": 2, "verdict": "good", "moment": "", "reply": "", "reply_zh": ""},
    ]
    r = impl["validate"](card(replies=replies), 2)
    assert r["status"] == "ok"
    assert r["prompt_version"] == VERSION
    assert r["total"] == 70
    assert r["scores"]["professionalism"] == 8
    assert r["replies"] == replies
    assert (r["dropped_replies"], r["clamped"]) == (0, 0)


def test_validate_clamps_and_counts(impl):
    r = impl["validate"](card(scores={"discovery": 11, "negotiation": -3}, total=150), 0)
    assert r["status"] == "ok"
    assert (r["scores"]["discovery"], r["scores"]["negotiation"], r["total"]) == (10, 0, 100)
    assert r["clamped"] == 3


def test_validate_drops_bad_replies_and_counts(impl):
    replies = [
        {"idx": 1, "verdict": "great", "moment": "", "reply": "", "reply_zh": ""},
        {"idx": 0, "verdict": "ok", "moment": "", "reply": "", "reply_zh": ""},
        {"idx": 3, "verdict": "ok", "moment": "", "reply": "", "reply_zh": ""},
        "junk",
        {"idx": 2, "verdict": "bad", "moment": "m", "reply": "", "reply_zh": ""},
    ]
    r = impl["validate"](card(replies=replies), 2)
    assert r["status"] == "ok"
    assert r["replies"] == [{"idx": 2, "verdict": "ok", "moment": "m", "reply": "", "reply_zh": ""}]
    assert r["dropped_replies"] == 4


def test_validate_reads_json_inside_other_text(impl):
    r = impl["validate"]("```json\n" + card(total=55) + "\n```", 0)
    assert (r["status"], r["total"]) == ("ok", 55)


# 第一次在 Dify 试跑时真实出现的超长示范回复（DeepSeek 只有 json_object，接口层不管 maxLength）
LONG_REPLY = (
    "On $2.95 FOB Ningbo, freight is on your side — I can quote sea freight to your port if you share it. "
    "If you take 5000 pcs at $2.80, I can look at sharing freight. Which do you prefer?"
)
LONG_REPLY_ZH = "宁波 FOB $2.95 运费由您承担——告诉我目的港，我可以报海运费。如果您订 5000 件、单价 $2.80，我可以考虑分担运费。您更倾向哪个？"


def bad(reply, reply_zh, moment="客户一压价就让到对方报的价"):
    return {"idx": 1, "verdict": "bad", "moment": moment, "reply": reply, "reply_zh": reply_zh}


def test_validate_cuts_long_reply_at_last_sentence_end(impl):
    assert len(LONG_REPLY) > 180
    r = impl["validate"](card(replies=[bad(LONG_REPLY, LONG_REPLY_ZH)]), 1)
    got = r["replies"][0]
    # "$2.95" 里的小数点不算句末；截到 180 以内最后一个句末，译文保留同样的句数
    assert got["reply"] == LONG_REPLY[: LONG_REPLY.index(" Which")]
    assert got["reply_zh"] == LONG_REPLY_ZH[: LONG_REPLY_ZH.index("您更倾向")]
    assert got["verdict"] == "bad"
    assert r["clamped"] == 1


def test_validate_cuts_reply_without_sentence_end_at_a_space(impl):
    reply = " ".join(["word"] * 50)
    r = impl["validate"](card(replies=[bad(reply, "译文")]), 1)
    got = r["replies"][0]
    assert len(got["reply"]) <= 180
    assert got["reply"] == " ".join(["word"] * 36)
    assert got["reply_zh"] == "译文"
    assert r["clamped"] == 1


def test_validate_hard_cuts_text_without_any_break(impl):
    replies = [{"idx": 1, "verdict": "ok", "moment": "啊" * 350, "reply": "", "reply_zh": ""}]
    r = impl["validate"](card(replies=replies), 1)
    assert r["replies"][0]["moment"] == "啊" * 300
    assert r["clamped"] == 1


def test_validate_cuts_long_translation_and_counts_each_field(impl):
    zh = "这是一句很长的翻译。" * 50
    r = impl["validate"](card(scores={"discovery": 12}, replies=[bad("Which port?", zh)]), 1)
    got = r["replies"][0]
    assert (got["reply"], got["reply_zh"]) == ("Which port?", "这是一句很长的翻译。" * 40)
    assert r["clamped"] == 2


def test_validate_keeps_texts_at_the_limit(impl):
    replies = [bad("x" * 179 + "?", "好" * 400, "评" * 300)]
    r = impl["validate"](card(replies=replies), 1)
    assert r["replies"] == replies
    assert r["clamped"] == 0


@pytest.mark.parametrize(
    "raw",
    [
        "",
        None,
        "no json here",
        "{not valid}",
        json.dumps({"total": 50, "replies": []}),
        json.dumps({"scores": {"responsiveness": 5}, "total": 50}),
        card(scores={"discovery": "high"}),
        card(total="n/a"),
    ],
)
def test_validate_unparseable(impl, raw):
    r = impl["validate"](raw, 1)
    assert r["status"] == "unparseable"
    assert r["detail"]
    assert (r["total"], r["scores"], r["replies"]) == (None, {}, [])
