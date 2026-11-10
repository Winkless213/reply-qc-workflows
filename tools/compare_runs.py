"""汇总多轮批量评分的结果：用量、延迟、重试、钳制、示范回复长度，以及每段样本的总分和逐条评级。

用法：python tools/compare_runs.py 标签=通配符 [标签=通配符 ...]
例：python tools/compare_runs.py code="out/code-fix*.jsonl" dify="out/dify-fix*.jsonl"
输入是 code-version/score.py 或 dify/run_batch.py 写出的 JSONL，每个文件一轮。
"""

import glob
import json
import statistics
import sys
from collections import Counter, defaultdict


def load(pattern):
    runs = []
    for path in sorted(glob.glob(pattern)):
        with open(path, encoding="utf-8") as f:
            runs.append([json.loads(line) for line in f if line.strip()])
    if not runs:
        sys.exit("没有匹配的文件：%s" % pattern)
    return runs


def tokens(rec):
    if "total_tokens" in rec:  # Dify：整次运行的用量
        return rec["total_tokens"]
    return (rec.get("usage") or {}).get("total_tokens")


def mean(values):
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def fmt(value, digits=0):
    return "-" if value is None else ("%.*f" % (digits, value))


def summarize(label, runs):
    recs = [r for run in runs for r in run]
    scored = [r for r in recs if tokens(r)]
    replies = [x for r in recs for x in ((r.get("result") or {}).get("replies") or []) if x.get("reply")]
    lengths = [len(x["reply"]) for x in replies]
    server = [r.get("elapsed_ms") for r in scored if r.get("elapsed_ms") is not None]
    return {
        "组": label,
        "轮数": str(len(runs)),
        "调模型次数": str(len(scored)),
        "状态": " ".join("%s×%d" % kv for kv in sorted(Counter((r.get("result") or {}).get("status") for r in recs).items())),
        "平均 token/次": fmt(mean(tokens(r) for r in scored)),
        "平均端到端 ms": fmt(mean(r["latency_ms"] for r in scored)),
        "平均服务端 ms": fmt(mean(server)) if server else "-",
        "重试次数": str(sum(len(r.get("retries") or []) for r in recs)),
        "钳制/丢弃": "%d/%d" % (
            sum((r.get("result") or {}).get("clamped") or 0 for r in recs),
            sum((r.get("result") or {}).get("dropped_replies") or 0 for r in recs),
        ),
        "格式约束": " ".join("%s×%d" % kv for kv in Counter(r.get("format_mode") for r in scored if "format_mode" in r).items()) or "-",
        "示范回复 条数/均长/最长": "%d/%s/%s" % (len(lengths), fmt(mean(lengths)), max(lengths) if lengths else "-"),
        ">120 / >180": "%d / %d" % (sum(n > 120 for n in lengths), sum(n > 180 for n in lengths)),
    }


def table(rows, keys):
    widths = {k: max(len(k), *(len(r[k]) for r in rows)) for k in keys}
    print(" | ".join(k.ljust(widths[k]) for k in keys))
    print("-+-".join("-" * widths[k] for k in keys))
    for r in rows:
        print(" | ".join(r[k].ljust(widths[k]) for k in keys))


def main(argv):
    groups = []
    for arg in argv[1:]:
        label, _, pattern = arg.partition("=")
        if not pattern:
            sys.exit(__doc__)
        groups.append((label, load(pattern)))
    if not groups:
        sys.exit(__doc__)

    rows = [summarize(label, runs) for label, runs in groups]
    for key in rows[0]:
        if key != "组":
            print("%-24s %s" % (key, "  ".join("%s: %s" % (r["组"], r[key]) for r in rows)))

    print("\n每段样本的总分（各轮）和逐条评级（g/o/b，各轮）：")
    per = defaultdict(dict)
    for label, runs in groups:
        for run in runs:
            for r in run:
                res = r.get("result") or {}
                cell = per[r["id"]].setdefault(label, {"totals": [], "verdicts": defaultdict(list)})
                cell["totals"].append("-" if res.get("total") is None else str(res["total"]))
                for x in res.get("replies") or []:
                    cell["verdicts"][x["idx"]].append(x["verdict"][0])
    out = []
    for sample in sorted(per):
        row = {"样本": sample}
        for label, _ in groups:
            cell = per[sample].get(label, {"totals": [], "verdicts": {}})
            verdicts = " ".join("#%d:%s" % (i, "".join(v)) for i, v in sorted(cell["verdicts"].items()))
            row[label] = "/".join(cell["totals"]) + ("  " + verdicts if verdicts else "")
        out.append(row)
    table(out, ["样本"] + [label for label, _ in groups])


if __name__ == "__main__":
    main(sys.argv)
