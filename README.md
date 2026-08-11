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
reasoning.effort: low
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

## 关键配置

```toml
[bilibili]
max_comment_pages = 2
max_video_pages = 10

[ark]
api_key = ""
base_url = "https://ark.cn-beijing.volces.com/api/v3/responses"
model = "doubao-seed-2-1-turbo-260628"
max_tokens = 2400
system_prompt = "你是B站UP主的评论回复助手。只回复语境清楚、有互动价值、低误判风险的评论；回复要自然、简短、具体，不要客服腔，不要编造事实。"

[reply]
enabled = true
max_process = 10
review_batch_size = 8
reply_delay = 2
chained_reply_enabled = true
```

`reply.enabled` 的含义是“启用自动生成审核草稿”，不是自动发送。

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

评论抓取沿用原项目的路径：先取账号视频列表，再逐个视频抓最近评论页。它不等同于创作中心“全账号最近回复流”的排序。测试两页后需要核对实际顺序；如果与创作中心差异明显，下一步应改用账号聚合回复流接口。

## 许可证

上游项目使用 MIT License，本分支保留原 LICENSE。
