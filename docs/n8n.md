# n8n 外围自动化：部署与运维

Dify 负责"评一段对话"，n8n 负责它外面的事：收对话、去重、调 Dify、把评分卡写进飞书多维表格、每日汇总、出错提醒。

## 工作流（`n8n/`）

| 工作流 | 触发 | 做什么 |
|---|---|---|
| 回复质检 · 评分入表 `score-to-table.json` | `POST /webhook/reply-qc`；每 30 分钟读收件箱 | 整理对话 → 按内容去重 → 调 Dify 评分 → 写飞书「评分卡」→ 记为已处理。单条失败记进错误表，下次触发自动重试 |
| 回复质检 · 每日汇总 `daily-summary.json` | 每天 18:07；`POST /webhook/reply-qc-daily` | 统计今天的评分卡 → 写飞书「每日汇总」、存 Markdown 到 `reports/`、推送到群 |
| 回复质检 · 出错记录 `error-log.json` | 上面两个工作流执行失败时 | 记进 n8n 错误表，推送到群 |
| 回复质检 · 初始化数据表 `setup-tables.json` | 部署脚本调用 | 建 n8n 数据表 `reply_qc_processed`、`reply_qc_errors` |
| 回复质检 · 清空数据表 `reset-tables.json` | 部署脚本调用 | 清空上面两张数据表，演示前重置用 |

## 数据在哪

- 飞书多维表格「评分卡」：每段对话一行，含状态、总分、五个维度、差评条数、最该改的一条和建议说法。
- 飞书多维表格「每日汇总」：每天一行。
- n8n 数据表 `reply_qc_processed`：处理成功的对话的去重键；`reply_qc_errors`：失败记录。
- `infra/n8n/files/inbox/`：放导出的对话 JSON；`infra/n8n/files/reports/`：每日汇总 Markdown。

## 部署

前提：Dify 已启动，评分工作流已发布并建好 API 密钥。n8n 要加入 Dify 的 `docker_default` 网络，所以先起 Dify。

1. `cd infra/n8n && docker compose up -d`，浏览器打开 http://localhost:5679 建管理员账号。n8n 助手的设置可以跳过。
2. 飞书（个人账号即可）：
   1. 开发者后台 https://open.feishu.cn/app →「创建企业自建应用」，在「凭证与基础信息」复制 App ID、App Secret。
   2. 「应用能力 > 添加应用能力」加「机器人」。
   3. 「开发配置 > 权限管理」开通「查看、评论、编辑和管理多维表格」（`bitable:app`）、「以应用的身份发消息」（`im:message:send_as_bot`）、「获取群组信息」。
   4. 「应用发布 > 版本管理与发布」创建版本并发布。
   5. 新建一个多维表格，右上角「...」→「...更多」→「添加文档应用」，添加这个应用并给「可管理」，复制表格链接。
   6. 在要接收通知的群里：「设置」→「群机器人」→「添加机器人」，选这个应用（不是「自定义机器人」）。
3. 按 `.env.example` 填 `.env`：`DIFY_API_KEY`、`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_BITABLE_URL`。
4. 依次运行：

   ```
   python tools/feishu_check.py                  # 凭据能换到令牌
   python tools/feishu_setup.py                  # 建「评分卡」「每日汇总」两张表，找到机器人所在的群，ID 写回 .env
   python tools/n8n_deploy.py setup-tables.json
   python tools/n8n_deploy.py --setup-tables     # 建 n8n 数据表
   python tools/n8n_deploy.py                    # 导入凭据和全部工作流并发布，15 秒内生效
   python tools/post_samples.py c04              # 飞书「评分卡」应出现一行
   ```

## 日常操作

- **推一段对话**：`POST http://127.0.0.1:5679/webhook/reply-qc`，请求体是一段对话、对话数组或 `{"conversations": [...]}`，格式同 `samples/conversations.json`。立即返回，评分在后台跑。
- **批量**：把导出的 JSON 文件放进 `infra/n8n/files/inbox/`，30 分钟内处理。文件不用挪走：内容没变的对话不会重评，来了新消息的会重评。
- **立刻出今天的汇总**：`POST http://127.0.0.1:5679/webhook/reply-qc-daily`，返回汇总内容。
- **演示前重置**：`python tools/n8n_deploy.py --reset-tables` 清空 n8n 的两张数据表，之后所有对话都会重新评分；飞书两张表里的行在飞书里删。
- **看失败**：n8n「数据表」里的 `reply_qc_errors`，或群里的出错提醒。失败的对话下次触发会自动重试。
- **在 n8n 里改了工作流**：用 n8n 的导出覆盖 `n8n/` 下对应文件。导出前把表 ID、群 ID 换回对应的 `__FEISHU_*__` 占位符。

## 已知限制

- webhook 没有加认证，端口只绑在 127.0.0.1。对外开放前要加 Header 认证。
- 去重只看对话内容；换了提示词版本不会自动重评。
- 每日汇总最多统计当天前 500 条评分卡。每跑一次就在「每日汇总」表里加一行，同一天手动触发几次就有几行；Markdown 文件按日期覆盖。
