"""把 n8n/ 下的工作流和 .env 里的密钥导入项目自带的 n8n 容器，用的是 n8n 官方 CLI。

用法：python tools/n8n_deploy.py [工作流文件名 ...]     不带参数时导入 n8n/ 下全部工作流
      python tools/n8n_deploy.py --setup-tables          建 n8n 数据表（首次部署跑一次，已存在则跳过）
      python tools/n8n_deploy.py --reset-tables          清空 n8n 的两张数据表（演示前重置，所有对话会重新评分）

- 密钥只写进临时文件，拷进容器导入后两边都删除，不打印。
- 工作流里的 __NAME__ 占位符用 .env 里同名变量替换；缺值的工作流跳过并说明。
- 导入后发布 PUBLISH 里的工作流；n8n 默认 15 秒内自动生效，不用重启。
"""

import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTAINER = "reply-qc-n8n-1"
PUBLISH = {"rqcScoreToTable1", "rqcDailySummary1", "rqcErrorLogger01"}
ONE_SHOT = {"--setup-tables": ("rqcSetupTables01", "reply-qc-setup-tables"),
            "--reset-tables": ("rqcResetTables01", "reply-qc-reset-tables")}


def read_env():
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
            env[key] = value.strip()
    return env


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True, encoding="utf-8", errors="replace")


def import_file(kind, text, name):
    """Copy one JSON file into the container, import it, then delete it on both sides."""
    fd, tmp = tempfile.mkstemp(suffix=".json")
    tmp = Path(tmp)
    target = "/tmp/" + name
    try:
        with open(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        docker("cp", str(tmp), CONTAINER + ":" + target)
        docker("exec", "-u", "root", CONTAINER, "chown", "node:node", target)
        result = docker("exec", CONTAINER, "n8n", "import:" + kind, "--input=" + target, check=False)
    finally:
        docker("exec", "-u", "root", CONTAINER, "rm", "-f", target, check=False)
        tmp.unlink(missing_ok=True)
    lines = (result.stdout + result.stderr).strip().splitlines()
    print("  import:%s %s -> exit %d  %s" % (kind, name, result.returncode, lines[-1] if lines else ""))
    return result.returncode == 0


def credentials(env):
    creds = []
    if env.get("DIFY_API_KEY"):
        creds.append({
            "id": "rqcDifyHeader001",
            "name": "Dify 回复质检评分 API",
            "type": "httpHeaderAuth",
            "data": {"name": "Authorization", "value": "Bearer " + env["DIFY_API_KEY"]},
        })
    if env.get("FEISHU_APP_ID") and env.get("FEISHU_APP_SECRET"):
        creds.append({
            "id": "rqcFeishuApp0001",
            "name": "飞书应用 回复质检",
            "type": "httpCustomAuth",
            "data": {"json": json.dumps({"body": {"app_id": env["FEISHU_APP_ID"], "app_secret": env["FEISHU_APP_SECRET"]}})},
        })
    return creds


def run_once(wid, path):
    """Data tables can only be changed by the running instance (n8n execute does not load that module):
    publish the workflow, call its webhook once, unpublish it again."""
    url = "http://127.0.0.1:5679/webhook/" + path
    print("publish:", docker("exec", CONTAINER, "n8n", "publish:workflow", "--id=" + wid, check=False).returncode)
    try:
        for _ in range(20):  # the instance picks up publications every 15 s
            time.sleep(3)
            try:
                with urllib.request.urlopen(urllib.request.Request(url, data=b"{}", method="POST"), timeout=30) as resp:
                    print("call ->", resp.status, resp.read().decode("utf-8")[:1500])
                    return 0
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    print("call ->", e.code, e.read().decode("utf-8")[:1500])
                    return 1
        print("webhook never registered")
        return 1
    finally:
        print("unpublish:", docker("exec", CONTAINER, "n8n", "unpublish:workflow", "--id=" + wid, check=False).returncode)


def main(argv):
    if len(argv) > 1 and argv[1] in ONE_SHOT:
        return run_once(*ONE_SHOT[argv[1]])

    env = read_env()
    creds = credentials(env)
    print("credentials:", [c["name"] for c in creds] or "none")
    if creds and not import_file("credentials", json.dumps(creds, ensure_ascii=False), "rqc-credentials.json"):
        return 1

    files = [ROOT / "n8n" / a for a in argv[1:]] or sorted((ROOT / "n8n").glob("*.json"))
    for path in files:
        text = path.read_text(encoding="utf-8")
        missing = sorted({k for k in re.findall(r"__([A-Z][A-Z0-9_]+)__", text) if not env.get(k)})
        if missing:
            print("skip %s: .env 缺少 %s" % (path.name, ", ".join(missing)))
            continue
        text = re.sub(r"__([A-Z][A-Z0-9_]+)__", lambda m: env[m.group(1)], text)
        workflow_id = json.loads(text)["id"]
        print(path.name, "->", workflow_id)
        if not import_file("workflow", text, path.name):
            return 1
        if workflow_id in PUBLISH:
            result = docker("exec", CONTAINER, "n8n", "publish:workflow", "--id=" + workflow_id, check=False)
            lines = (result.stdout + result.stderr).strip().splitlines()
            print("  publish -> exit %d  %s" % (result.returncode, lines[-1] if lines else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
