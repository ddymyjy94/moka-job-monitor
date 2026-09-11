# Moka 招聘岗位监控

监控 Moka 平台上多家公司的招聘岗位：每天自动爬取近一个月发布的岗位，写入飞书多维表格，并用 AI 生成分析，最后通过飞书机器人推送消息（全量分析 + 新增岗位提醒）。

## 监控的公司

公司清单在 [companies.json](companies.json) 中配置，新增公司只需加一条配置，无需改代码：

```json
{
  "id": "公司英文标识",
  "name": "公司中文名",
  "url": "https://app.mokahr.com/... 公司Moka招聘页链接",
  "cities": ["城市1", "城市2"],
  "enabled": true
}
```

- `cities` 为空数组表示不限城市
- `enabled` 设为 `false` 可临时停用某家公司
- 飞书表格中每家公司自动创建两个标签页：`{公司名}-岗位`、`{公司名}-分析`

## 运行方式

### 云端定时（GitHub Actions，推荐）

每天北京时间 9:00 自动运行，电脑无需开机。

- 运行记录见仓库的 **Actions** 标签页
- 手动触发：Actions → 招聘岗位监控 → Run workflow
- 修改定时时间：编辑 [.github/workflows/monitor.yml](.github/workflows/monitor.yml) 中的 cron 表达式（注意 cron 用 UTC 时间，北京时间 = UTC + 8）：

| 北京时间 | cron 表达式 |
| --- | --- |
| 每天 8:00 | `0 0 * * *` |
| 每天 9:00（当前） | `0 1 * * *` |
| 每天 12:00 | `0 4 * * *` |
| 每天 18:00 | `0 10 * * *` |
| 每天 21:00 | `0 13 * * *` |

> GitHub 定时任务可能有几分钟延迟，属正常现象。

### 本地运行（Windows + Edge）

```
python monitor.py
```

或双击 `启动招聘网页.bat`。本地凭证从 `.env` 文件读取（不提交到仓库）。

## 密钥配置

云端在仓库 Settings → Secrets and variables → Actions 中配置，本地写在 `.env`：

| 变量 | 说明 |
| --- | --- |
| FEISHU_APP_ID | 飞书应用 App ID |
| FEISHU_APP_SECRET | 飞书应用 App Secret |
| FEISHU_SPREADSHEET_TOKEN | 飞书表格 token（表格 URL 中获取） |
| FEISHU_USER_OPEN_ID | 消息接收人的 open_id |
| AI_API_URL | AI 接口地址（默认智谱） |
| AI_API_KEY | AI 接口密钥 |
| AI_MODEL | AI 模型名（默认 glm-4-flash） |

飞书前提：在表格文档右上角「…」→「添加文档应用」中授权该应用，云端才能用 tenant_access_token 读写表格和发消息。

## 快照与新增判断

每次爬取的岗位快照保存在 `jobs_data/{公司id}/jobs_时间戳.json`，与上次快照对比得出新增岗位。云端每次运行后会自动把新快照提交回本仓库，保证跨天的增量对比正常。
