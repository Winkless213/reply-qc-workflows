"""Where do Dify's ~2 s per run go? Run c07 (no model call) in blocking and streaming mode and time both.

usage: python overhead_probe.py <conversations.json> <rounds>   (env DIFY_BASE_URL, DIFY_API_KEY)
"""
import json
import os
import sys
import time
import urllib.request

convs = {c["id"]: c for c in json.load(open(sys.argv[1], encoding="utf-8"))}
conv = convs["c07"]
inputs = {
    "customer_name": conv["customer_name"],
    "messages": json.dumps(conv["messages"], ensure_ascii=False),
    "note": "", "bottom_lines": "", "knowledge": "",
}
url = os.environ["DIFY_BASE_URL"].rstrip("/") + "/workflows/run"
headers = {"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["DIFY_API_KEY"]}


def call(mode):
    body = json.dumps({"inputs": inputs, "response_mode": mode, "user": "overhead-probe"}).encode("utf-8")
    t0 = time.perf_counter()
    marks = {}
    server = None
    with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=60) as resp:
        marks["headers"] = time.perf_counter() - t0
        if mode == "blocking":
            data = json.load(resp)["data"]
            server = data.get("elapsed_time")
        else:
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line[5:])
                marks.setdefault(ev.get("event"), time.perf_counter() - t0)
                if ev.get("event") == "workflow_finished":
                    server = ev["data"].get("elapsed_time")
    total = time.perf_counter() - t0
    return total, server, marks


for mode in ("blocking", "streaming"):
    for _ in range(int(sys.argv[2])):
        total, server, marks = call(mode)
        detail = " ".join("%s=%.0fms" % (k, v * 1000) for k, v in marks.items())
        print("%-9s total=%.0fms server=%.0fms | %s" % (mode, total * 1000, (server or 0) * 1000, detail))
