# B站评论人工审核回复工具

这是基于 [Janson20/BiliCommentBot](https://github.com/Janson20/BiliCommentBot) 改造的本地审核版。原项目负责 B站登录、评论抓取、楼中楼定位、限频和 Web UI；本分支把“生成后立即发送”改成了“豆包生成草稿 → 人工勾选 → 明确确认 → 批量发送”。

当前是测试阶段，默认只读取每个视频最近 `2` 页评论。验证稳定后可在配置页改成 `8`～`10` 页。

## 安全边界

- 豆包只给语境清楚、有互动价值、低误判风险的评论生成回复。
- 上下文不足、争议大、容易引战或只能写万能套话的评论会标为跳过。
- 页面同时展示视频标题、观众原评论、楼中楼上级评论、豆包回复原文和筛选理由。
- Codex 不会二次改写豆包回复。
- 后台监控只生成审核草稿，绝不自动发送。
- 只有勾选为“已批准”的草稿才可发送。
- 发送前页面会再次确认：`将向 B站发送已勾选的 N 条豆包原文`。
- API 也要求明确提交非空的评论 ID 列表，不支持空列表或遗漏列表时批量发送。

## 模型

固定使用火山方舟 Responses API：

```text
model: doubao-seed-2-1-turbo-260628
endpoint: https://ark.cn-beijing.volces.com/api/v3/responses
reasoning.effort: medium（设置页可选 low / medium / high）
max_output_tokens: 128000
```

API Key 优先从环境变量读取：

```powershell
$env:ARK_API_KEY = "你的方舟 API Key"
```

也兼容 `VOLCENGINE_ARK_API_KEY`。不要把真实 Key 写入仓库。

## 本地运行

```powershell
python -m pip install -r requirements.txt
python main.py
```

然后打开 `http://127.0.0.1:5000`：

1. 在“登录”页扫码获取 B站 Cookie。
2. 在“配置 → 豆包 / 火山方舟”确认模型和 API 地址。
3. 打开“回复审核”，点击“生成最近评论草稿”。
4. 检查原评论、上级评论和豆包原文，只勾选确认无误的回复。
5. 点击“发送已勾选”，核对数量后确认。

也可以启动“草稿监控”，让程序定时补充审核草稿。它不会绕过人工审核发送。

## 双账号

- 账号 1：双击 `启动机器人.bat`，使用端口 `5000` 和项目根目录中现有的登录数据。
- 账号 2：双击 `启动账号2-5001.bat`，使用端口 `5001` 和 `data-account-2` 独立数据目录。

两个实例的 Cookie、UID、审核草稿、历史、缓存和日志互不共用。不要把一个账号的数据目录交给另一个实例。

也可以通过环境变量自定义：

```powershell
$env:BILI_PORT = "5001"
$env:BILI_ACCOUNT_NAME = "账号2"
$env:BILI_DATA_DIR = "$PWD\data-account-2"
python main.py
```

## 关键配置

```toml
[bilibili]
max_comment_pages = 10
max_video_pages = 10

[ark]
api_key = ""
base_url = "https://ark.cn-beijing.volces.com/api/v3/responses"
model = "doubao-seed-2-1-turbo-260628"
max_tokens = 128000
reasoning_effort = "medium"
request_interval_seconds = 0.15
max_concurrency = 16
max_retries = 5
system_prompt = "你是B站UP主的评论回复助手。只回复语境清楚、有互动价值、低误判风险的评论；回复要自然、简短、具体，不要客服腔，不要编造事实。"

[reply]
enabled = true
max_process = 10
review_since = "" # 例如 2026-08-10T00:00；留空表示只按条数限制
review_batch_size = 4
reply_delay = 2
chained_reply_enabled = true
```

`reply.enabled` 的含义是“启用自动生成审核草稿”，不是自动发送。

正常审核从创作中心“评论管理”的账号评论流读取，按评论时间从新到旧跨视频排列。
`max_process` 是最新评论扫描上限，默认 10，界面提供 10 到 50000 的完整档位。
开发和调试时可设置 `BILI_REVIEW_HARD_LIMIT=110` 临时收紧后端上限，正式发布不设置
该变量；`review_since` 是可选的
起始时间。两者同时设置时，先碰到哪个边界就停止。已有审核草稿、已经发送、
UP 已回复及账号自己的评论不会再次交给豆包，除非手动点击单条“重新生成”。

## 数据文件

| 文件 | 用途 |
|---|---|
| `config.toml` | 本地配置 |
| `review_drafts.json` | 审核草稿、批准状态和发送状态 |
| `history.json` | 已成功发送的回复历史 |
| `bilibili_cookie.json` | B站 Cookie |
| `video_cache.json` | 视频列表缓存 |

## 验证

```powershell
python -m py_compile bot.py server.py
python -m unittest discover -s tests -v
```

测试会 mock B站发送接口，不会真实回复评论。

## 当前限制

正常审核只读取视频评论（创作中心“全部视频评论”，`type=1`）。配置“仅处理指定
BV”时，仍保留原项目的单视频评论抓取路径。

## 许可证

上游项目使用 MIT License，本分支保留原 LICENSE。
