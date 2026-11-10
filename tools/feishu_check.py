"""检查飞书自建应用是否配好：能换到 tenant_access_token，能读到 FEISHU_BITABLE_URL 指向的多维表格。

用法：python tools/feishu_check.py      读 .env 的 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_BITABLE_URL；不打印令牌和密钥
"""

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://open.feishu.cn/open-apis"


def read_env():
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            env[key.strip()] = value.strip()
    return env


def call(method, path, token=None, body=None):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8") or "{}") | {"http_status": e.code}


def tenant_token(env):
    r = call("POST", "/auth/v3/tenant_access_token/internal", body={"app_id": env["FEISHU_APP_ID"], "app_secret": env["FEISHU_APP_SECRET"]})
    if r.get("code") != 0:
        sys.exit("换令牌失败：code=%s msg=%s" % (r.get("code"), r.get("msg")))
    return r["tenant_access_token"]


def app_token(env):
    m = re.search(r"/base/([A-Za-z0-9]+)", env.get("FEISHU_BITABLE_URL", ""))
    if not m:
        sys.exit("FEISHU_BITABLE_URL 不是 feishu.cn/base/ 开头的多维表格链接")
    return m.group(1)


def main():
    env = read_env()
    token = tenant_token(env)
    print("tenant_access_token: ok")
    meta = call("GET", "/bitable/v1/apps/" + app_token(env), token)
    if meta.get("code") != 0:
        print("读不到多维表格：code=%s msg=%s（检查「添加文档应用」是否加了这个应用并给了可管理）" % (meta.get("code"), meta.get("msg")))
        return 1
    print("多维表格：", meta["data"]["app"].get("name"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
