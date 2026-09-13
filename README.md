# University Physics Agent

面向大学物理学习的本地优先工具。0.6.0 Windows v0 和 0.5.0 WSL/Linux 版均为 GitHub
**Pre-release（预览版）**；它们用于先行试用可审计知识检索、分层提示、受控完整解答和本地学习记录，
还不是完整课程平台。

## Windows v0（优先试用）

Windows 10/11 x64 学生可下载
[`university-physics-agent-0.6.0-windows-x64-v0.zip`](https://github.com/amazingand/university-physics-agent/releases/tag/v0.6.0)，
解压后双击 `windows\launch-physics-agent.cmd`。首次需要已安装的 Python 3.12 x64，并会在明确提示后
联网安装本项目既有依赖；不需要 WSL 或 Codex。打开后先点“案例 A”或“案例 C”即可本地试用。
脚本支持 `py -3.12`、PATH 中的 `python` 或 `PHYSICS_AGENT_PYTHON` 指定的解释器。

完整步骤与限制见 [Windows v0 快速开始](docs/student/WINDOWS-V0.md)。

## 使用条件

- 0.6.0 Windows v0 面向 Windows 10/11 x64；0.5.0 继续支持 Linux/WSL。
- 学生只需 Python/Conda 环境，不需要安装或使用 Codex。
- 案例 A/C、知识检索、题目导入和学习记录可在本地运行；配置 DeepSeek 的案例 B、`chat` 或 `teach` 才会联网。
- 下载预览资产请访问 [GitHub Releases](https://github.com/amazingand/university-physics-agent/releases)。

## 下载、校验与安装

从 v0.5.0 Pre-release 下载 bundle、wheel 和 `.sha256` sidecar。先在下载目录校验归档：

```bash
sha256sum -c university-physics-agent-0.5.0-wsl-linux.tar.gz.sha256
tar -xzf university-physics-agent-0.5.0-wsl-linux.tar.gz
cd university-physics-agent-0.5.0-wsl-linux
sha256sum -c SHA256SUMS
```

再创建环境并安装 bundle 内的 wheel：

```bash
conda env create -f source/environment.yml
conda run -n physics-agent python -m pip install --no-deps artifacts/university_physics_agent-0.5.0-py3-none-any.whl
conda run -n physics-agent python -m physics_agent --version
conda run -n physics-agent physics-agent config-check
```

发布清单和逐文件摘要位于 bundle 的 `release-manifest.json` 与 `SHA256SUMS`。若 sidecar 或清单校验失败，请停止安装并重新下载。

## 三个可复制案例

案例 A：粗糙面题的条件完整性检查。它会在本地识别运动状态和摩擦参数不足，不调用模型。

```bash
conda run -n physics-agent physics-agent demo case-a
```

案例 B：DeepSeek 分层提示。先创建本地配置并通过环境变量注入 Key：

```bash
mkdir -p cache exports
cp config/local.example.toml config/local.toml
read -rsp 'DeepSeek API Key: ' PHYSICS_AGENT_CHAT_API_KEY; echo
export PHYSICS_AGENT_CHAT_API_KEY
conda run -n physics-agent physics-agent demo case-b --config config/local.toml --hint-level 1
```

需要完整解答时显式申请；完整解答仍受证据、物理一致性和工具门禁约束：

```bash
conda run -n physics-agent physics-agent demo case-b --config config/local.toml --full-solution --max-tokens 1024
unset PHYSICS_AGENT_CHAT_API_KEY
```

案例 B 会把题目和必要上下文发送给 DeepSeek，可能产生费用。网络错误或限流触发的重试也可能重复计费；请先确认供应商账户、价格和限额。Key 只应存在于环境变量或未提交的本地配置中，不要写进题目、日志、截图或 Issue。

案例 C：在本地确定性验证 `72 km/h = 20 m/s`，记录一次单位错误并主动导出 JSON 学习包。

```bash
mkdir -p exports
conda run -n physics-agent physics-agent demo case-c --export exports/case-c-learning.json
conda run -n physics-agent physics-agent learning show --learner-id anon_demo_case_c_001 --package-id mechanics.zh.reviewed --package-version 0.2.0 --import exports/case-c-learning.json
```

## 其他本地命令

```bash
mkdir -p cache exports/questions
conda run -n physics-agent physics-agent knowledge-check --package-root knowledge/mechanics-zh-reviewed-0.2.0 --index cache/reviewed.sqlite3 --package-id mechanics.zh.reviewed --package-version 0.2.0
conda run -n physics-agent physics-agent question-import examples/questions/net-force-concept-draft.yaml --output-dir exports/questions
```

`chat` 是没有物理一致性与来源发布门禁的原始通路；面向学生的受限入口是 `teach`，且其完整解答只覆盖当前力学白名单。M3 草稿知识不能用于教学，第八版教材映射仍待完成；没有本地模型时也不能声称完整离线教学。0.5.0 不承诺完整课程覆盖、自动评分、整卷批改、图像题处理、动画、Web UI 或任意题型的可靠求解。云端实测只覆盖发布说明所列固定案例，不能外推到所有输入。

## 隐私与安全

- 本地案例、导出、题库导入和学习记录默认留在本机。
- 只有显式选择云端案例时，必要输入才会发往 DeepSeek；发送前请删除姓名、学号、联系方式、密钥和未获授权的教材内容。
- 不要导入私人 PPTX、受限教材或学生敏感数据，也不要把生成结果当作未经核对的标准答案。
- 详细边界见 [隐私与限制](docs/student/PRIVACY-LIMITS.md)；常见问题见 [故障排查](docs/student/TROUBLESHOOTING.md)。

## 文档

- [快速开始](docs/student/QUICKSTART.md)
- [Windows v0 快速开始](docs/student/WINDOWS-V0.md)
- [隐私与限制](docs/student/PRIVACY-LIMITS.md)
- [故障排查](docs/student/TROUBLESHOOTING.md)
- [可复现重建](docs/student/REBUILD.md)

## 许可与反馈

- 原创代码：MIT License，版权人余杰（2026）。
- 审核知识包：CC BY-SA 4.0。
- 第三方依赖和材料：分别遵循其自身许可证，详见发布包中的 `NOTICE` 与 `THIRD_PARTY_NOTICES`。

问题请提交到 [GitHub Issues](https://github.com/amazingand/university-physics-agent/issues)。Issue 是公开内容：不要提交 API Key、学生信息、私人教材、完整云端请求/响应或其他敏感信息。
