"""在 .env 的 FEISHU_BITABLE_URL 指向的多维表格里建「评分卡」「每日汇总」两张表，再找到应用机器人所在的群，
把表 ID 和群 ID 写回 .env（n8n_deploy.py 用它们替换工作流里的占位符）。重复运行只补缺的，不会重复建表。

用法：python tools/feishu_setup.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from feishu_check import ROOT, app_token, call, read_env, tenant_token  # noqa: E402

TEXT, NUMBER, SELECT, DATE = 1, 2, 3, 5
MINUTE = {"date_formatter": "yyyy/MM/dd HH:mm"}

TABLES = {
    "评分卡": ("FEISHU_SCORE_TABLE_ID", [
        ("对话", TEXT), ("状态", SELECT), ("总分", NUMBER),
        ("响应及时性", NUMBER), ("需求挖掘", NUMBER), ("议价与守底线", NUMBER), ("风险处理", NUMBER), ("专业度", NUMBER),
        ("差评条数", NUMBER), ("我方回复数", NUMBER),
        ("最该改的一条", TEXT), ("建议说法", TEXT), ("建议说法中文", TEXT),
        ("评分时间", DATE, MINUTE), ("提示词版本", TEXT), ("Dify运行ID", TEXT), ("去重键", TEXT), ("说明", TEXT),
    ]),
    "每日汇总": ("FEISHU_DAILY_TABLE_ID", [
        ("日期", TEXT), ("评分条数", NUMBER), ("平均总分", NUMBER, {"formatter": "0.0"}), ("低于60分", NUMBER),
        ("差评条数", NUMBER), ("失败条数", NUMBER), ("最低分对话", TEXT), ("汇总", TEXT), ("生成时间", DATE, MINUTE),
    ]),
}


def set_env(key, value):
    path = ROOT / ".env"
    lines = path.read_bytes().decode("utf-8").split("\n")
    for i, line in enumerate(lines):
        if line.split("=", 1)[0] == key:
            lines[i] = key + "=" + value
            break
    else:
        lines.insert(len(lines) - 1 if lines[-1] == "" else len(lines), key + "=" + value)
    path.write_bytes("\n".join(lines).encode("utf-8"))


def main():
    env = read_env()
    app = app_token(env)
    token = tenant_token(env)

    meta = call("GET", "/bitable/v1/apps/" + app, token)
    if meta.get("code") != 0:
        sys.exit("读不到这个多维表格：code=%s msg=%s（检查「添加文档应用」是否加了「回复质检」并给了可管理）" % (meta.get("code"), meta.get("msg")))
    print("多维表格：", meta["data"]["app"].get("name"))
    set_env("FEISHU_APP_TOKEN", app)

    listed = call("GET", "/bitable/v1/apps/%s/tables?page_size=100" % app, token)
    existing = {t["name"]: t["table_id"] for t in listed.get("data", {}).get("items", [])}
    for name, (key, fields) in TABLES.items():
        if name in existing:
            table_id = existing[name]
            print("已有表 %s -> %s" % (name, table_id))
        else:
            body = {"table": {"name": name, "default_view_name": "全部", "fields": [
                {"field_name": f[0], "type": f[1], **({"property": f[2]} if len(f) > 2 else {})} for f in fields
            ]}}
            r = call("POST", "/bitable/v1/apps/%s/tables" % app, token, body)
            if r.get("code") != 0:
                sys.exit("建表 %s 失败：code=%s msg=%s" % (name, r.get("code"), r.get("msg")))
            table_id = r["data"]["table_id"]
            print("新建表 %s -> %s（%d 个字段）" % (name, table_id, len(fields)))
        set_env(key, table_id)

    chats = call("GET", "/im/v1/chats?page_size=50", token)
    if chats.get("code") != 0:
        sys.exit("读不到机器人所在的群：code=%s msg=%s（检查应用是否加了机器人能力、开了「获取群组信息」并发布了版本）" % (chats.get("code"), chats.get("msg")))
    items = chats["data"].get("items", [])
    chosen = [c for c in items if c["chat_id"] == env.get("FEISHU_CHAT_ID")] or items
    if len(chosen) != 1:
        listing = "；".join("%s %s" % (c["name"], c["chat_id"]) for c in items) or "无"
        sys.exit("机器人所在的群：%s。把应用机器人拉进接收通知的群；在多个群里时，在 .env 填 FEISHU_CHAT_ID 指定一个" % listing)
    print("通知群：", chosen[0]["name"])
    set_env("FEISHU_CHAT_ID", chosen[0]["chat_id"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
