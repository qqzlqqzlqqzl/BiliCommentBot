# B站评论人工审核助手

这是基于 `Janson20/BiliCommentBot` 改造的本地 Windows 工具。默认流程只有一条：

1. 从 B站创作中心评论时间线按“新到旧”读取评论。
2. 豆包判断是否值得回复，并给出可直接发送的候选原文。
3. 用户逐条检查和勾选。
4. 再次确认后，只把本次勾选的候选串行发送到 B站。

后台监控也只能补充审核草稿，不能绕过人工批准自动发送。

## 直接使用 EXE

正式发布目录是：

```text
dist\BiliCommentReviewer\
```

双击其中的 `BiliCommentReviewer.exe` 即可。不要只复制 EXE；整个目录需要一起保留。

- 第一次启动会在 `%LOCALAPPDATA%\BiliCommentReviewer` 创建产品数据目录。
- 应用自动使用空闲的本地端口，不需要手动管理 5000/5001。
- 再次双击会打开已经运行的实例，不会再启动一套后台。
- 一个应用内可以添加多个账号；Cookie、草稿、历史、缓存和日志按账号隔离。
- 已保存的 Cookie、Refresh Token、API Key 和密码哈希不会回显到浏览器页面。

首次使用：

1. 在左侧选择或添加账号。
2. 打开“登录”，使用 B站 App 扫码。
3. 在“配置 → 豆包”填写火山方舟 API Key，确认模型和提示词。
4. 打开“回复审核”，选择读取数量和可选时间范围。
5. 生成草稿，检查原评论、父评论和豆包候选。
6. 只勾选确认无误的项目，再点击“发送已勾选”。

## 读取范围

- 默认读取 `10` 条，避免误操作消耗大量 Token。
- 正式产品提供 `10、20、50、100、200、300、500、1000、2000、5000、10000、50000` 档位。
- 时间范围可留空。数量和时间同时设置时，先碰到哪个边界就停止。
- 已有草稿、已经发送、UP 已回复及账号自己的评论默认不会再次交给豆包。
- 单条“重新生成”是显式例外，只有用户点击时才再次消耗 Token。

开发调试可临时设置 `BILI_REVIEW_HARD_LIMIT=110` 收紧后端上限。正式 EXE
启动时会主动清除该变量，发布能力仍是 50000。

## 多账号和旧数据导入

左侧“导入旧账号”可以复制旧版数据目录。支持：

- `config.toml`
- `bilibili_cookie.json`
- `review_drafts.json`
- `history.json`
- `video_cache.json`

导入会创建新的账号目录，不修改、不删除旧目录。旧版的 `启动机器人.bat` 和
`启动账号2-5001.bat` 只保留给源码兼容调试，不进入正式产品工作流。

## 豆包调用

默认配置：

```text
model: doubao-seed-2-1-turbo-260628
endpoint: https://ark.cn-beijing.volces.com/api/v3/responses
reasoning.effort: medium
max_output_tokens: 128000
```

每个请求会带上系统提示词、视频标题、评论作者、观众评论、是否追评、必要的父级
上下文，以及重新生成时需要避开的旧候选。豆包只返回 `should_reply`、候选原文和
跳过理由；程序不二次改写候选。

豆包生成可以按配置受控并发，并通过请求间隔和 429 退避控制速率。B站抓取和发送
始终串行，发送间隔由 B站请求配置控制。

API Key 可保存到当前账号数据目录，也可使用环境变量：

```powershell
$env:ARK_API_KEY = "你的火山方舟 API Key"
```

环境变量优先于账号配置。不要把真实 Key、Cookie 或产品数据目录提交到仓库。

## 安全边界

- 上下文不足、争议大、容易引战、敏感或只能写万能套话的评论会建议跳过。
- 页面展示视频、作者、评论时间、原评论、父评论、豆包原文和判断理由。
- 未勾选的草稿不能发送。
- 发送 API 必须收到明确、非空的评论 ID 列表。
- 发送前页面再次显示本次发送数量并要求确认。
- 上次退出时处于发送中的项目会标记为“发送结果待核对”，不会自动重发。
- 生成或发送期间不能切换、添加或导入账号。

## 从源码运行

源码调试：

```powershell
python -m pip install -r requirements.txt
python main.py
```

默认地址为 `http://127.0.0.1:5000`。源码模式仍兼容旧环境变量和 BAT，但正式使用
建议运行 EXE。

## 构建 Windows 发布包

```powershell
pwsh -NoProfile -File .\build_release.ps1
```

重复构建且依赖未变化时：

```powershell
pwsh -NoProfile -File .\build_release.ps1 -SkipInstall
```

构建脚本使用独立 `.venv-build`，旧 build/dist 目标会送入回收站，不会直接删除。

## 验证

```powershell
python -m py_compile account_manager.py bot.py server.py product_app.py
python -m unittest discover -s tests
pwsh -NoProfile -File .\smoke_test_release.ps1
```

`smoke_test_release.ps1` 使用临时产品目录，检查实际 EXE 启动、50000 正式档位、
本地静态脚本、双账号配置隔离和单实例复用；测试完成后把临时数据送入回收站。
单元测试和烟测不会真实发送 B站评论。

最终的 100 条真实生成验收只生成审核草稿，不包含发送；必须使用用户已有的登录和
豆包配置，并由用户明确开始。

## 许可证

上游及本项目使用 MIT License。发布包包含的 Socket.IO JavaScript 客户端说明见
`THIRD_PARTY_NOTICES.md`。
