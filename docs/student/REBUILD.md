# 可复现重建与校验

本说明面向希望独立核验 v0.5.0 GitHub Pre-release 的 Linux/WSL 用户。Windows 请通过 WSL（推荐 VS Code Remote WSL）操作；重建不需要 Codex。

## 先核验下载资产

```bash
sha256sum -c university-physics-agent-0.5.0-wsl-linux.tar.gz.sha256
tar -xzf university-physics-agent-0.5.0-wsl-linux.tar.gz
cd university-physics-agent-0.5.0-wsl-linux
sha256sum -c SHA256SUMS
```

同时检查 `release-manifest.json` 中记录的版本、source commit、文件路径和摘要。任何不一致都应故障关闭，不要继续安装。

## 从 bundle 安装并做最小验证

```bash
conda env create -f source/environment.yml
conda run -n physics-agent python -m pip install --no-deps artifacts/university_physics_agent-0.5.0-py3-none-any.whl
conda run -n physics-agent python -m physics_agent --version
conda run -n physics-agent physics-agent config-check
conda run -n physics-agent physics-agent demo case-a
```

这些命令只验证本地安装和案例 A，不会调用 DeepSeek。

## 从公开 tag 重建 wheel

发布说明会给出精确的公开 commit。请在干净、受信任的公开仓库工作树中检出 `v0.5.0`，确认它解析到该 commit，再执行：

```bash
git checkout --detach v0.5.0
git status --short
git rev-parse v0.5.0^{commit}
conda run -n physics-agent python -m pip wheel --no-build-isolation --no-deps --no-cache-dir --wheel-dir dist .
sha256sum dist/university_physics_agent-0.5.0-py3-none-any.whl
```

tag 解析结果必须与发布说明中的公开 commit 一致，`git status --short` 应为空。`PUBLIC-SOURCE.json` 另行记录构建来源提交，用于把隔离公开快照绑定到已验收构建；该开发提交不作为公开历史发布。重建 wheel 的摘要应与发布资产记录比较；不同构建工具版本可能影响归档字节，因此还应对照发布说明中的验证环境版本。

## 可复现性含义

发布流程从同一固定 commit 在两个全新 staging 中独立构建，并比较 wheel、bundle 清单、逐文件摘要、归档和 sidecar。只有逐字节一致的结果才应发布。公开 bundle 采用固定 allowlist，不应包含私人 PPTX、密钥、本机绝对路径、未跟踪配置或学生数据。

DeepSeek 调用不属于离线重建步骤。若你另行运行云端案例，输入会发送给 DeepSeek且可能计费；重试也可能产生额外费用。

发现摘要或内容异常时，请到 [GitHub Issues](https://github.com/amazingand/university-physics-agent/issues) 报告资产名、公开摘要和去敏后的复现步骤。不要提交 API Key、学生信息、私人材料或完整云端请求/响应。
