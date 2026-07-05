# GitHub 自动推送 Docker Hub 部署步骤

## 1. 准备 Docker Hub

1. 登录 Docker Hub。
2. 确认仓库存在：`gvddfdf/ovh-bot`。
3. 在账号安全设置里创建 Access Token。

不要把 Docker Hub 账号密码写进 GitHub，只使用 Access Token。

## 2. 配置 GitHub Secrets

进入 GitHub 仓库：

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

添加两个 Secret：

| 名称 | 值 |
|------|------|
| `DOCKERHUB_USERNAME` | Docker Hub 用户名，例如 `gvddfdf` |
| `DOCKERHUB_TOKEN` | Docker Hub Access Token |

## 3. 推送后自动构建

仓库根目录的 `.github/workflows/docker-publish.yml` 会在推送到 `master` 后自动构建：

```text
ovh-bot/Dockerfile
```

并推送镜像：

```text
DOCKERHUB_USERNAME/ovh-bot:latest
DOCKERHUB_USERNAME/ovh-bot:master
DOCKERHUB_USERNAME/ovh-bot:sha-xxxxxxx
```

## 4. 发布版本镜像

需要发布版本时：

```bash
git tag v2.1.0
git push origin v2.1.0
```

会额外推送：

```text
DOCKERHUB_USERNAME/ovh-bot:v2.1.0
```

## 5. 服务器部署

进入 `ovh-bot` 目录并复制环境变量示例：

```bash
cp .env.example .env
```

编辑 `.env`：

```bash
DOCKER_IMAGE=gvddfdf/ovh-bot:latest
```

启动：

```bash
docker compose up -d
docker compose logs -f
```

更新镜像：

```bash
docker compose pull
docker compose up -d
```
