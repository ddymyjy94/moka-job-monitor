# Moka 招聘岗位监控（多公司多平台）

每天北京时间 9 点自动爬取多家公司的招聘岗位（近一个月内发布），
每天推送一条极简「岗位速报」，每周一额外推送 AI 生成的「每周速览」。
- 数据源：按 `platform` 分发——`moka`（Moka 页，Selenium 浏览器）/ `hotjob`（大易平台，纯接口）/
  `beisen`（北森平台，纯接口）；接口类无需浏览器，速度更快
- 运行环境：GitHub Actions（免费、无需开电脑）
- 输出方式：每天一条飞书消息（日速报）；周一增加一条 AI 周报；飞书表格仅作静默数据档案
- 监控范围：`companies.json` 配置驱动，可按城市过滤，新增公司不改代码
- 提醒规则：有新增岗位的公司标 🔥 并列出新岗位 + 近 7 天岗位；无新增只占一行
---

## 一、工作原理

1. 读取 `companies.json`，按 `platform` 分发爬取器：`moka` 开浏览器爬页面；
   `hotjob` / `beisen` 直接调平台的职位列表 HTTP 接口（无需浏览器）
2. 按配置城市过滤：优先用 URL `location` 参数；页面不认参数时（如施耐德）自动改为
   干净 URL 加载后点选城市复选框
3. 爬取近 30 天岗位：名称、发布日期、城市、部门、岗位描述
4. 安全过滤兜底：非监控城市的岗位一律剔除，绝不入表
5. 与上次快照（`jobs_data/{公司id}/`）对比，得出**新增岗位**
6. 岗位表静默写入飞书 `{公司名}-岗位` 标签页（按发布日期倒序，写前清空防残留），
   作为数据档案，日常无需查看
7. 汇总发送**一条**「岗位速报」（所有公司合并）：有新增的公司标 🔥，
   岗位列表展示近 7 天发布的岗位（日期倒序，含今日新增）；无新增只占一行
8. 每周一额外调用 AI 生成每公司周报，覆盖写入 `{公司名}-分析` 标签页，
   并合并发送一条「每周速览」
9. 把本次快照提交回仓库，供下次运行对比

---

## 二、配置步骤

### 0. 公司配置字段说明（companies.json）

| 字段 | 说明 |
|---|---|
| `platform` | `moka`（默认，浏览器爬取）/ `hotjob`（大易接口，URL 含 SU 段 siteCode）/ `beisen`（北森接口，取 URL 域名根调接口） |
| `url` | 招聘页链接，各平台爬取器自动从中提取所需参数 |
| `cities` | 监控城市列表，爬取后做安全过滤兜底（岗位城市含任一城市才保留） |
| `priority` | 可选，数字越小越靠前（1 最高）；速报/周报/表格均按此排序，未配置排最后 |
| `filters`（仅 beisen） | 直接合并进北森接口请求体，如 `{"ClassificationTwo": ["9"]}` 只看"总部招聘"分类 |

新增公司只需复制一条现有配置改字段，**不改代码**。

### 1. 准备飞书应用与表格

| 准备项 | 获取方式 |
|--------|-----------|
| 飞书自建应用 | [飞书开放平台](https://open.feishu.cn) → 创建企业自建应用，拿到 `App ID` / `App Secret` |
| 应用权限 | 开通 `sheets:spreadsheet`（读写表格）、`im:message`（发消息）权限并发布版本 |
| 目标表格 | 新建一个电子表格，URL 中 `sheets/` 后那串就是 `SPREADSHEET_TOKEN` |
| 文档授权 | 表格右上角「…」→「**添加文档应用**」→ 搜索并添加该应用（云端免浏览器授权的关键） |
| 消息接收人 | 应用的「可用范围」加入自己；open_id 可通过调用 API 获取 |

### 2. 把项目推送到 GitHub

私有仓库即可（本仓库：`ddymyjy94/moka-job-monitor`）。

### 3. 配置 GitHub Secrets

仓库页面 → **Settings** → **Secrets and variables** → **Actions**，
依次添加以下 7 个（本地运行则写在 `.env` 文件里，格式 `键=值`）：

| Secret 名称 | 值 |
|-------------|----|
| `FEISHU_APP_ID` | 飞书应用 App ID |
| `FEISHU_APP_SECRET` | 飞书应用 App Secret |
| `FEISHU_SPREADSHEET_TOKEN` | 飞书表格 token |
| `FEISHU_USER_OPEN_ID` | 消息接收人 open_id |
| `AI_API_URL` | AI 接口地址（默认智谱 `https://open.bigmodel.cn/api/paas/v4/chat/completions`） |
| `AI_API_KEY` | AI 接口密钥 |
| `AI_MODEL` | AI 模型名（默认 `glm-4-flash`） |

### 4. 手动触发测试

进入仓库 → **Actions** → 左侧选 `招聘岗位监控` → **Run workflow** → 点击运行。
约 6~8 分钟跑完，验证：飞书表格出现新标签页、收到飞书消息、Actions 最后一步
`回写岗位快照` 有 commit。之后每天北京时间 9 点自动运行。

---

## 三、本地测试（Windows + Edge）

```powershell
# .env 里写好 7 个变量后
python monitor.py
# 或双击 启动招聘网页.bat
```

本地用 Edge + 本地驱动（`edgedriver_win64/`），需与 Edge 版本匹配：
驱动从 `https://msedgedriver.microsoft.com/{版本}/edgedriver_win64.zip` 下载。

---

## 四、文件说明

```
.
├── .github/workflows/monitor.yml  # GitHub Actions 定时任务（每天 9 点）
├── monitor.py                     # 爬取 + 城市筛选 + 飞书 + AI 主脚本
├── companies.json                 # 监控公司清单（配置驱动）
├── jobs_data/{公司id}/            # 岗位快照，运行后自动 commit 回写
├── requirements.txt               # 云端依赖（本地已装勿执行）
├── 启动招聘网页.bat               # 本地运行入口
└── README.md
```

---

## 五、注意事项

- **定时延迟**：GitHub Actions 免费层的 cron 可能有几分钟延迟，属正常现象。
- **城市筛选差异**：博世校招页认 URL location 参数；施耐德社招页必须用干净 URL
  点选复选框。脚本已内置混合策略 + 安全过滤兜底，无需人工干预。
- **快照回写冲突**：云端每次运行都会自动 commit 快照，本地直接 push 经常被拒。
  习惯"先 `git pull --rebase` 再 `git push`"即可。
- **网络代理**：git 推送 GitHub 需走本机 Clash 代理（127.0.0.1:7890），直连会被重置。
- **免费额度**：私有仓库每月 2000 分钟 Actions，本脚本每次约 7 分钟，足够。
- **Edge 自动升级**：本地 Edge 升级后驱动不匹配会报 SessionNotCreated，按上面链接
  下载对应版本驱动替换。

---

## 六、日常维护备忘

### 1. 改什么、改哪里对照表

| 想改什么 | 改哪里 | 是否要 push 代码 | 是否立即生效 |
|---|---|---|---|
| **新增 / 停用监控公司** | [`companies.json`](companies.json)（复制一条改 id/name/url） | ✅ | ✅ 下次运行即用新值 |
| **监控城市** | [`companies.json`](companies.json) 的 `cities` | ✅ | ✅ 下次运行生效 |
| **临时停用某公司** | [`companies.json`](companies.json) 的 `enabled` 改 `false` | ✅ | ✅ 下次运行生效 |
| **定时时间** | [`.github/workflows/monitor.yml`](.github/workflows/monitor.yml) 的 `cron:` 行 | ✅ | 下次到点才生效 |
| **消息接收人** | GitHub Secrets 的 `FEISHU_USER_OPEN_ID` | ❌ | ✅ 下次运行即用新值 |
| **换 AI 模型** | GitHub Secrets 的 `AI_MODEL` | ❌ | ✅ 下次运行即用新值 |
| **换目标表格** | GitHub Secrets 的 `FEISHU_SPREADSHEET_TOKEN`（新表格要重新授权文档应用） | ❌ | ✅ 下次运行生效 |

### 2. 改定时的具体步骤（网页即可，不用 git）

打开仓库 → `.github/workflows/monitor.yml` → 铅笔图标编辑 → 改 `cron:` 行 → Commit changes。

**时区换算**：cron 用 UTC，北京时间 = UTC + 8（即北京小时数 − 8）。

| 想要的北京时间 | cron 写法 |
|---|---|
| 每天 8:00 | `0 0 * * *` |
| 每天 9:00（当前） | `0 1 * * *` |
| 每天 12:00 | `0 4 * * *` |
| 每天 18:00 | `0 10 * * *` |
| 每天 21:00 | `0 13 * * *` |

格式为 `分 时 * * *`，如北京 8:30 → `30 0 * * *`。

### 3. 修改代码 / 配置后的标准 push 流程

```powershell
git add <改的文件>
git commit -m "chore: 描述改动"
git pull --rebase origin main   # 先拉云端 Actions 自动 commit 的快照，避免冲突
git push origin main             # 自动走 Clash 7890 代理
```

### 4. 没收到飞书消息时的排查清单

1. **看 Actions 运行日志**：仓库 → Actions → 点最近一次运行 → 看「运行监控」步骤输出：
   - `飞书消息推送成功！` → 消息已发出，检查飞书是否屏蔽了应用消息
   - `请先配置 FEISHU_USER_OPEN_ID` → Secrets 漏配
   - `[公司名] 处理出错: ...` → 该公司爬取失败，看具体报错
   - 某公司步骤整段没出现 → 页面加载失败或超时
2. **检查文档授权**：表格右上角「…」→「添加文档应用」，确认应用在列表里（最常见原因）
3. **检查 GitHub Secrets**：Settings → Secrets and variables → Actions，7 个都在且无拼写错误
4. **手动触发验证**：Actions → Run workflow，跑完看日志再对照上表定位
