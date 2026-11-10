"""Dify 版批量评分：逐段调用已发布工作流的 Service API（POST /v1/workflows/run，流式），
记录结束节点的统一结果、节点重试、异常和用量，输出格式与 code-version/score.py 对齐。

用法：python dify/run_batch.py samples/conversations.json out/dify.jsonl
环境变量：DIFY_BASE_URL（含 /v1，如 http://127.0.0.1/v1；Windows 上别用 localhost，会多等约 2 秒）、DIFY_API_KEY（应用的 API 密钥）。
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

USER = "reply-qc-batch"


def inputs_of(conv):
    # 工作流开始节点的五个输入；messages 以 JSON 字符串传入，由"渲染与脱敏"节点解析
    return {
        "customer_name": conv.get("customer_name") or "",
        "messages": json.dumps(conv.get("messages") or [], ensure_ascii=False),
        "note": conv.get("note") or "",
        "bottom_lines": conv.get("bottom_lines") or "",
        "knowledge": conv.get("knowledge") or "",
    }


def events(resp):
    """逐个产出 SSE 流里 data 行的 JSON（ping 等无 data 的行跳过）。"""
    for raw in resp:
        line = raw.decode("utf-8").strip()
        if line.startswith("data:") and line[5:].strip():
            yield json.loads(line[5:])


def run(conv, base, key):
    record = {
        "id": conv.get("id"),
        "run_id": None,
        "workflow_status": None,
        "latency_ms": 0,
        "elapsed_ms": None,
        "total_tokens": None,
        "exceptions_count": None,
        "retries": [],
        "node_errors": [],
        "error": None,
        "raw": None,  # 评分节点（LLM）的原文，用来核对校验节点丢弃、钳制和截断了什么
        "raw_truncated": None,  # Dify 在流式事件里截短过大的节点输出时为 true
        "result": None,
    }
    body = {"inputs": inputs_of(conv), "response_mode": "streaming", "user": USER}
    req = urllib.request.Request(
        base.rstrip("/") + "/workflows/run",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            for ev in events(resp):
                kind, data = ev.get("event"), ev.get("data") or {}
                if kind == "workflow_started":
                    record["run_id"] = ev.get("workflow_run_id") or data.get("id")
                elif kind == "node_retry":
                    record["retries"].append(
                        {"node": data.get("title"), "retry_index": data.get("retry_index"), "error": data.get("error")}
                    )
                elif kind == "node_finished":
                    if data.get("node_type") == "llm":
                        record["raw"] = (data.get("outputs") or {}).get("text")
                        record["raw_truncated"] = data.get("outputs_truncated")
                    if data.get("status") != "succeeded":
                        record["node_errors"].append(
                            {"node": data.get("title"), "status": data.get("status"), "error": data.get("error")}
                        )
                elif kind == "workflow_finished":
                    record["workflow_status"] = data.get("status")
                    record["elapsed_ms"] = round((data.get("elapsed_time") or 0) * 1000)
                    record["total_tokens"] = data.get("total_tokens")
                    record["exceptions_count"] = data.get("exceptions_count")
                    record["error"] = data.get("error")
                    record["result"] = (data.get("outputs") or {}).get("result")
                elif kind == "error":
                    record["error"] = ev.get("message") or json.dumps(ev, ensure_ascii=False)
    except urllib.error.HTTPError as e:
        record["error"] = "HTTP %d: %s" % (e.code, e.read().decode("utf-8", "replace")[:500])
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        record["error"] = "%s: %s" % (type(e).__name__, e)
    record["latency_ms"] = round((time.perf_counter() - started) * 1000)
    return record


def main(argv):
    if len(argv) != 3:
        sys.exit(__doc__)
    base, key = os.environ.get("DIFY_BASE_URL"), os.environ.get("DIFY_API_KEY")
    if not base or not key:
        sys.exit("缺少环境变量 DIFY_BASE_URL 或 DIFY_API_KEY")
    src, dst = Path(argv[1]), Path(argv[2])
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("w", encoding="utf-8", newline="\n") as out:
        for conv in json.loads(src.read_text(encoding="utf-8")):
            record = run(conv, base, key)
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            result = record["result"] or {}
            print(
                record["id"],
                record["workflow_status"],
                result.get("status"),
                result.get("total"),
                "retries=%d" % len(record["retries"]),
                "%dms" % record["latency_ms"],
                "ERROR" if record["error"] else "",
            )


if __name__ == "__main__":
    main(sys.argv)
