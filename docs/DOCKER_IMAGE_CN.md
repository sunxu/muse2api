# Docker 镜像构建与运行（muse2api）

镜像：`ghcr.io/sunxu/muse2api`。CI 在原生 GitHub Linux `amd64` 与 `arm64` Runner 分别构建，通过后才发布 `latest` 和 `sha-<main-commit>` 多架构标签。PR 只构建、不推送镜像。构建不使用任何真实 Muse 账号、Cookie 或代理密码。

## 从 GHCR 拉取

```bash
docker pull --platform linux/arm64 ghcr.io/sunxu/muse2api:latest  # Apple Silicon
# 或（Linux x86_64）
docker pull --platform linux/amd64 ghcr.io/sunxu/muse2api:latest
```

如 GHCR 包保持私有，先使用具备 `read:packages` 权限的 GitHub token 执行 `docker login ghcr.io`；如包已设置为 public 则无需登录。镜像是否已发布，请以对应的 GitHub Actions 成功状态为准。

## 本机启动（API 仅绑定本机）

```bash
mkdir -p data
chmod 700 data
# MUSE2API_KEY 请仅在本机环境变量中配置；勿把真实密钥提交到仓库。
docker run -d --name muse2api --restart unless-stopped \
  --shm-size=2g \
  -p 127.0.0.1:18610:18610 \
  -e MUSE2API_KEY="$MUSE2API_KEY" \
  -v "$PWD/data:/app/data" \
  ghcr.io/sunxu/muse2api:latest
```

访问 `http://127.0.0.1:18610/admin`。镜像内包含 Python、Chromium 和 `proxy_bridge.py`，支持无需认证或用户名密码认证的 HTTP、SOCKS5 上游代理。

## Docker Desktop for macOS 代理注意事项

- **容器里的 `127.0.0.1` 是容器自己**，无法直接表示 Mac 上的 `127.0.0.1:20240` 或 `127.0.0.1:65220`。
- 使用 Docker Desktop 容器访问 Mac 宿主机时可尝试 `host.docker.internal`，如 `http://host.docker.internal:20240`。但如果宿主代理服务只绑定 Mac 的 `127.0.0.1`，仍可能无法通过宿主网关访问；必须先确认端口监听范围及安全转发配置，**不要直接将本地无认证代理开放到公网或局域网**。
- 不要在 Docker 和 macOS 之间复制现有 `accounts.json`，除非明确需要迁移且理解其中包含未加密 Cookie 和代理凭据。
- 容器 Chromium 当前使用 `--no-sandbox`，不适合暴露给不可信多租户或公网；CDP 调试端口也不得发布。
- `--shm-size=2g` 为 Chromium 提供共享内存。验证期间建议保持单实例、限制监听地址。

## 从源代码本地构建

```bash
git checkout main
docker build --platform linux/arm64 -t muse2api:local .
```

## 验收边界

Docker 构建与 Python 模块导入不等于真实 Muse 业务验收。真实账号、视频、流式 Responses、持续并发及代理故障注入应分别测试。构建镜像不读取任何 Muse 凭据。
