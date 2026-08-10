# 具身大模型科研日报 · 按需深度解析

在现有「每日 arXiv 初筛 + 飞书推送」的基础上，新增了**按需深度解析**能力：

- 在 GitHub Actions 界面（或任何调用 workflow_dispatch 的方式）填入 arXiv ID 或任意网页链接
- 自动抓取全文 → Qwen 深度解析 → 生成**每篇独立的网页**（`archive/{id}.html`）→ 报告正文直接**推送到飞书群**（应用机器人）→ 网页同步到 GitHub Pages

不需要常驻服务、不需要公网服务器，全程跑在 GitHub 的免费算力上。

---

## 一、怎么用

### 方式 1：GitHub Actions 界面（推荐，手机也可以操作）

1. 打开仓库 → **Actions** → 左侧 **Daily VLA Report** → **Run workflow**
2. 在 **papers** 输入框填内容（支持以下几种，逗号、换行、空格分隔均可）：

| 输入 | 说明 |
|---|---|
| `2602.10116` | arXiv ID |
| `https://arxiv.org/html/2602.10116` / `/abs/...` / `/pdf/...` | arXiv 链接（自动提取 ID） |
| `arxiv:2501.12345` | arXiv 前缀形式 |
| `https://xxx.com/blog/xxx` | 任意网页 / blog / technical report（会抓正文解读） |

例：`2602.10116, https://example.com/blog/vla-survey`

3. （可选）**chat_id** 留空用仓库默认群；填了则本次报告推送到指定群
4. 点 **Run workflow**，约 1-3 分钟后完成

### 方式 2：命令行 / 脚本触发

```bash
# 本地或任意有 GitHub token 的地方
curl -X POST \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/RedBeanCake/daily-robot-report/actions/workflows/daily.yml/dispatches \
  -d '{"ref":"main","inputs":{"papers":"2602.10116, 2602.99999"}}'
```

> 你的 GitHub token 需要仓库的 `Actions` 写权限（Personal Access Token，勾选 `repo` 或 `workflow` scope）。

---

## 二、解析结果去哪了

- **网页**：每篇一个独立页面 `archive/{id}.html`（URL 解读按内容哈希命名，如 `archive/49ed0450d6.html`），自动收录进首页 `index.html` 索引
  - Pages 地址：`https://RedBeanCake.github.io/daily-robot-report/`
- **飞书群**：报告正文（Qwen 生成的完整 Markdown 报告）以消息卡片直接发到群，网页链接在卡片里

---

## 三、部署与配置（首次使用需要）

### 1. GitHub 仓库 Secrets

仓库 → **Settings → Secrets and variables → Actions**，添加：

| Secret | 必填 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | ✅ | 阿里云百炼 DashScope API Key，解析全靠它 |
| `FEISHU_WEBHOOK` | 建议 | 飞书自定义机器人 webhook（定时初筛推送用） |
| `FEISHU_APP_ID` | 推送报告正文到群里时需要 | 飞书自建应用 App ID |
| `FEISHU_APP_SECRET` | 推送报告正文到群里时需要 | 飞书自建应用 App Secret |

### 2. 飞书侧：让机器人能往群里发消息

如果你希望**报告正文直接推到飞书群**（而不是只推卡片/文本），需要：

1. [飞书开放平台](https://open.feishu.cn/app) 创建**自建应用**
2. 应用开启「机器人」能力
3. 权限管理里开通：`im:message`（发送消息）、`im:message:send_as_bot`（以应用身份发消息）
4. 发布应用版本，等待管理员审核通过
5. 把机器人拉进目标群
6. 获取群 `chat_id`：给机器人发一条消息后，调用
   `GET https://open.feishu.cn/open-apis/im/v1/chats?page_size=20`（带 tenant_access_token）查询，或者用调试台 `https://open.feishu.cn/api-explorer` 查看
7. 把 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 填进 Secrets，把 chat_id 作为 workflow 的 **chat_id 输入**（或以后做成默认值）

> 没有应用机器人也没关系：不填 App ID/Secret 时，报告会退回用 `FEISHU_WEBHOOK` 以文本形式推送（需要先去飞书群 → 设置 → 群机器人 → 添加自定义机器人，把 webhook 存进 Secrets）。

### 3. GitHub Pages

仓库 → **Settings → Pages** → Source 选择 `Deploy from a branch` → 分支选 `gh-pages`。（workflow 里已配置自动部署，首次需要手动确认一次 Pages 设置。）

---

## 四、和原版行为的差异

| 项目 | 原版 | 现在 |
|---|---|---|
| 手动解析输入 | 仅 arXiv ID（`paper_ids`） | arXiv ID / 链接 / 任意网页 URL（`papers`） |
| 归档页面 | 同一天多次解析互相覆盖 | 每篇独立一页，全部进索引 |
| 推送内容 | 只推「查看网页」卡片 | 报告正文直接推到飞书群 |
| 手动模式副作用 | 还会抓 arXiv 列表、触发初筛 | 只解析你输入的内容，不碰初筛 |
| 定时初筛 | ✅ 保留 | ✅ 不变（`cs.RO`，每天 10:00 北京时间） |

---

## 五、文件结构

```
main.py          # 抓取 + Qwen 解析 + 生成页面 + 飞书推送
daily.yml        # GitHub Actions 工作流
index.html       # 解析存档索引（自动重建）
archive/*.html   # 每篇论文/网页的解析页（自动生成）
```

---

## 六、常见问题

- **解析失败 / 没收到飞书消息**：看 Actions 运行日志（`Run Scraper` 步骤）。常见原因：DashScope Key 过期、网页反爬抓不到正文、arxiv HTML 未生成时自动退回摘要解析。
- **网页抓取不干净**：目前抓 `<article>` / `<main>` 正文，剔除了导航页脚。对个别站点可能抓到不理想，可在 `get_webpage_text()` 里针对该站点加规则。
- **Qwen 模型**：当前默认 `qwen3.7-max-2026-05-17`，在 `main.py` 里搜 `model=` 可换（注释里列了可选型号）。
