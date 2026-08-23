# BiliCommentReviewer 0.2.0

发布日期：2026-08-23

Windows x64 预发布版，基于 `Janson20/BiliCommentBot` 的本地评论审核工作流 fork。

## 主要变化

- 按 B站创作中心最新评论时间线从新到旧读取，支持读取数量和时间范围。
- 豆包负责筛选和生成候选；支持人工审核、重新生成、人工不回复和可选自动发送。
- 已回复、人工不回复、不可回复页面和历史草稿均有明确状态，避免重复处理。
- 支持应用内多账号，配置、草稿、历史和日志按账号隔离。
- 多账号后台自动轮次全局串行，不同账号之间至少等待 10 分钟。
- B站评论发送始终串行，默认发送间隔 10 秒。
- 支持账号迁移 ZIP：同 UID 合并回复历史和草稿，不覆盖目标电脑现有凭据。
- 提供 Windows EXE 和固定本地地址，无需安装 Python。

## 安全边界

- 自动回复默认关闭。
- 账号运行、生成或发送期间禁止迁移。
- 账号迁移 ZIP 包含 Cookie 和豆包 API Key，不包含在本 GitHub Release 中。
- GitHub Release 只提供干净程序包及 SHA256 校验文件。

## 验证

- 151 项自动化测试通过。
- 正式 EXE 启动、固定端口、双账号、自动任务互锁和迁移去重闭环烟测通过。
- Windows `FileVersion` 和 `ProductVersion` 均为 `0.2.0`。

## 下载校验

`BiliCommentReviewer-0.2.0-windows-x64.zip`

SHA256：

`251E18340CDCCBE83DCC35150F794691AB75D5D70A2C85CFA2F30BCDAA300F1B`
