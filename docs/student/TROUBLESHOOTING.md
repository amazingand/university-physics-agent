# 故障排查

v0.5.0 是仅支持 Linux/WSL 的 GitHub Pre-release。Windows 用户应在 WSL 终端中执行命令，推荐 VS Code Remote WSL；学生不需要 Codex。

## `sha256sum` 校验失败

不要继续解压或安装。确认 bundle 与 sidecar 来自同一 v0.5.0 Release，删除损坏文件后重新下载。若 `release-manifest.json` 或 `SHA256SUMS` 中任一摘要不匹配，也应停止使用。

## 找不到 `physics-agent`

先确认环境和 wheel：

```bash
conda env list
conda run -n physics-agent python -m physics_agent --version
```

如果模块可运行而控制台命令不可用，重新按快速开始安装 bundle 内的 wheel，不要改用系统 Python。

## `config-check` 失败

```bash
conda run -n physics-agent physics-agent config-check
```

检查配置路径、TOML 拼写和文件权限。默认本地案例不需要 Key；云端案例应使用显式本地配置：

```bash
cp config/local.example.toml config/local.toml
conda run -n physics-agent physics-agent config-check --config config/local.toml
```

## DeepSeek Key 缺失或无效

```bash
read -rsp 'DeepSeek API Key: ' PHYSICS_AGENT_CHAT_API_KEY; echo
export PHYSICS_AGENT_CHAT_API_KEY
conda run -n physics-agent physics-agent demo case-b --config config/local.toml --hint-level 1
```

不要把 Key 放进 Issue、截图或日志。鉴权失败时先检查环境变量名称、账户状态和本地配置；不要盲目重复请求。云端调用与失败后的重试都可能计费。

## 网络错误、超时或限流

先确认 WSL 内网络可用，再检查供应商状态、账户限额和超时配置。自动重试是有上限的，但仍可能产生额外费用。若错误持续，停止重试并保留去敏后的错误类别和时间，不要公开完整请求/响应。

## 完整解答被拒绝

这是安全门禁的预期行为。缺少审核证据、来源标记、必要工具结果或物理一致性校验时，系统会拒绝发布完整解答。可先请求较低层级提示并检查题目条件；不要通过伪造引用或修改输出文件绕过门禁。

## 题目导入失败

检查输入是否为支持的 YAML/JSON、字段是否符合 schema、标识符是否重复，以及目标目录是否可写。只导入具有明确许可且不含个人信息的题目。

## `chat` 或 `teach` 没有覆盖我的课程内容

0.5.0 的 `chat` 没有物理一致性与来源发布门禁；面向学生的受限入口是 `teach`，完整解答也只覆盖当前力学白名单。M3 草稿知识不能用于教学，第八版教材映射仍待完成；没有本地模型时不能声称完整离线教学。完整课程、整卷批改、图像题、动画、自动评分和 Web UI 不在本预览版范围内。这通常不是安装故障。

仍无法解决时，请到 [GitHub Issues](https://github.com/amazingand/university-physics-agent/issues) 提交最小复现步骤、版本号和去敏后的错误信息。Issue 是公开内容，禁止提交 API Key、学生数据、私人教材或完整云端请求/响应。
