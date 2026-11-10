"""把样本对话推给 n8n 的评分入表 webhook。

用法：python tools/post_samples.py [样本 id ...]     不带参数时推全部样本
例：  python tools/post_samples.py c04 c07
"""

import json
import sys
import urllib.request
from pathlib import Path

URL = "http://127.0.0.1:5679/webhook/reply-qc"
samples = json.loads((Path(__file__).resolve().parent.parent / "samples" / "conversations.json").read_text(encoding="utf-8"))
wanted = sys.argv[1:]
picked = [c for c in samples if not wanted or c["id"] in wanted]
body = json.dumps({"conversations": picked}, ensure_ascii=False).encode("utf-8")
req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
with urllib.request.urlopen(req, timeout=30) as resp:
    print(resp.status, resp.read().decode("utf-8"), "| sent:", [c["id"] for c in picked])
