# Windows v0 快速开始

版本：0.6.0 Pre-release

## 需要什么

- Windows 10 或 Windows 11，x64；
- Python 3.12 x64（保留 Tcl/Tk；可使用 `py` launcher、PATH 中的 `python`，或显式解释器路径）；
- 首次安装既有 Python 依赖时需要访问 PyPI；运行本地案例 A/C 后不联网；
- 不需要 WSL，也不需要 Codex。

## 启动

1. 下载并解压 `university-physics-agent-0.6.0-windows-x64-v0.zip` 到可写目录。
2. 双击 `windows\launch-physics-agent.cmd`。
3. 首次运行会明确提示联网安装；输入 `Y` 后创建包内 `.venv` 并启动窗口。
4. 点击“案例 A”查看条件不足分析，或点击“案例 C”查看 `72 km/h = 20 m/s` 的本地工具结果。

以后再次双击同一文件即可启动。若首次安装中断，重新双击会继续安装，不需要删除整个解压目录。
若 Python 未加入 PATH 且没有 `py` launcher，可先在命令提示符中执行
`set "PHYSICS_AGENT_PYTHON=C:\你的路径\python.exe"`，再从同一窗口运行启动脚本。

## 可选 DeepSeek

本地案例不需要 Key。若要使用输入框的云端教学路径，在命令提示符中临时设置 Key，并从同一窗口启动：

```bat
set "PHYSICS_AGENT_CHAT_API_KEY=你的Key"
windows\launch-physics-agent.cmd
set "PHYSICS_AGENT_CHAT_API_KEY="
```

题目和必要上下文会发送给 DeepSeek并可能产生费用。GUI v0 固定最多一次请求尝试；点击停止后会先显示
“正在取消”，已经发送的同步请求结束后才显示“已停止”，不会自动重发。不要把 Key、姓名、学号、私人
教材或完整学生回答写入文件或 GitHub Issue。

## 数据与限制

- 会话消息只保存在当前内存，关闭窗口后不自动恢复。
- 仅在你主动选择文件时导入或导出版本化结构化学习 JSON；不会把完整聊天、原始回答、内部推理或 Key 写入学习包。
- 当前学生可用内容仍是局部力学知识和案例 A/C；完整解答只覆盖既有受限力学白名单。
- 这是 Tk 原生桌面预览，不是自包含单文件 EXE；首次需要 Python 与网络安装既有依赖。
- Windows 10/11 的真实桌面运行证据分别记录；未取得的系统不冒充已验收。
- 尚无图像题、Web UI、整卷批改、自动评分或大学物理 1、2 全课程覆盖。
