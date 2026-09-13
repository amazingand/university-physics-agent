# 快速开始（v0.5.0 预览版）

v0.5.0 是 GitHub Pre-release，仅支持 Linux/WSL。Windows 用户请在 WSL 中操作，推荐 VS Code Remote WSL。学生不需要 Codex。

## 1. 校验和安装

从 [GitHub Releases](https://github.com/amazingand/university-physics-agent/releases) 下载 v0.5.0 bundle、wheel 与 `.sha256` sidecar，在下载目录执行：

```bash
sha256sum -c university-physics-agent-0.5.0-wsl-linux.tar.gz.sha256
tar -xzf university-physics-agent-0.5.0-wsl-linux.tar.gz
cd university-physics-agent-0.5.0-wsl-linux
sha256sum -c SHA256SUMS
conda env create -f source/environment.yml
conda run -n physics-agent python -m pip install --no-deps artifacts/university_physics_agent-0.5.0-py3-none-any.whl
conda run -n physics-agent python -m physics_agent --version
conda run -n physics-agent physics-agent config-check
```

`sha256sum` 必须显示 `OK`。bundle 内还应包含 `release-manifest.json` 和 `SHA256SUMS`；任何摘要不匹配都应视为文件损坏或被替换。

## 2. 案例 A：粗糙面条件检查

```bash
conda run -n physics-agent physics-agent demo case-a
```

该案例会在本地识别运动状态和摩擦参数不足，返回需要补充信息，不调用模型。

## 3. 案例 B：DeepSeek 分层提示

```bash
mkdir -p cache exports
cp config/local.example.toml config/local.toml
read -rsp 'DeepSeek API Key: ' PHYSICS_AGENT_CHAT_API_KEY; echo
export PHYSICS_AGENT_CHAT_API_KEY
conda run -n physics-agent physics-agent demo case-b --config config/local.toml --hint-level 1
```

完整解答必须显式申请：

```bash
conda run -n physics-agent physics-agent demo case-b --config config/local.toml --full-solution --max-tokens 1024
```

案例 B 会向 DeepSeek 发送题目和必要上下文，可能产生费用；失败后的重试也可能再次计费。请在运行前检查供应商价格、余额和限额。不要把 Key 写入题目、日志或 Git；使用结束后可执行 `unset PHYSICS_AGENT_CHAT_API_KEY`。

## 4. 案例 C：单位换算与学习包导出

```bash
mkdir -p exports
conda run -n physics-agent physics-agent demo case-c --export exports/case-c-learning.json
conda run -n physics-agent physics-agent learning show --learner-id anon_demo_case_c_001 --package-id mechanics.zh.reviewed --package-version 0.2.0 --import exports/case-c-learning.json
```

## 5. 审核知识、题目与学习记录

```bash
mkdir -p cache exports/questions
conda run -n physics-agent physics-agent knowledge-check --package-root knowledge/mechanics-zh-reviewed-0.2.0 --index cache/reviewed.sqlite3 --package-id mechanics.zh.reviewed --package-version 0.2.0
conda run -n physics-agent physics-agent question-import examples/questions/net-force-concept-draft.yaml --output-dir exports/questions
```

导入前仍应确认题目来源与许可。不要导入私人 PPTX、受限教材或包含学生身份信息的材料。

## 6. 预览版边界

`chat` 是没有物理一致性与来源发布门禁的原始通路；面向学生的受限入口是 `teach`，且完整解答只覆盖当前力学白名单。M3 草稿知识不能用于教学，第八版教材映射仍待完成；没有本地模型时不能声称完整离线教学。0.5.0 不提供完整课程覆盖、自动评分、整卷批改、图像题识别、动画或 Web UI。模型输出可能有误，重要结论应回到题目条件、单位和审核来源复核。

更多说明见 [隐私与限制](PRIVACY-LIMITS.md)、[故障排查](TROUBLESHOOTING.md) 和 [可复现重建](REBUILD.md)。需要反馈时使用 [GitHub Issues](https://github.com/amazingand/university-physics-agent/issues)，但不要提交 Key、学生数据、私人材料或完整云端请求/响应。
