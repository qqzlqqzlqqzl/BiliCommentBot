# 旧版工具

这里的文件只保留给历史兼容、问题排查和上游对照，不是正式产品入口。

- `auto_launcher.py`：旧的一键下载/启动器，会自行克隆项目。
- `config_gui.py`：已弃用的 Tkinter 配置界面。
- `test.py`：旧单文件测试脚本；当前测试在根目录 `tests/`。
- `rate_limit_monitor.py`、`README_rate_limit.md`：旧限流监控实验。
- `启动机器人.bat`、`启动账号2-5001.bat`：旧源码双端口入口。

正式使用请运行根目录构建出的
`dist\BiliCommentReviewer\BiliCommentReviewer.exe`。源码开发入口仍是根目录
`main.py`，正式单元测试使用 `python -m unittest discover -s tests`。

这些文件不会进入正式发布包。旧 BAT 仍指向根目录的 `launch_instance.py`，
仅用于兼容调试；多账号正式流程由单个 EXE 内部管理。
