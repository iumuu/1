# 维护说明

## 当前安全默认值

- Telegram 访问默认关闭。请配置 `telegram.allowed_users`；只有确认是可信私有 Bot 时，才设置 `allow_all_users = true`。
- 独立监控脚本 `monitor.py` 默认只通知，不自动下单，也就是 `auto_buy = false`。
- `monitor.py` 已补充明确的命令行参数；修改自动化行为前，先运行 `python monitor.py --help` 查看用法。
- `monitor.py` 会把实际有货的内存和硬盘配置传给 `quick_buy()`，避免下成别的配置。
- 未设置 `OVH_BOT_DATA_DIR` 时，本地 Bot 状态会写入 `./data/watch_tasks.json`。
- Docker 镜像使用非 root 的 `app` 用户运行，并且 `.dockerignore` 会排除 `.env`、`config.toml`、`data/` 等敏感或运行时文件。

## 部署前检查

1. 复制 `.env.example` 为 `.env`，或复制 `config.example.toml` 为 `config.toml`。
2. 把 `TG_ALLOWED_USERS` 或 `telegram.allowed_users` 设置为你的 Telegram 用户 ID。
3. 保持 `TG_ALLOW_ALL_USERS=false`，除非你明确要让所有人都能操作这个 Bot。
4. 初次部署建议保持 `auto_buy = false`，先确认通知和配置选择都正确，再按需开启自动结账。
5. OVH API 权限按实际功能授予。下单和服务器管理需要较宽的写权限，务必保管好凭证。

## 验证方式

部署前先跑语法检查：

```bash
python -m py_compile bot.py monitor.py
```

Docker 部署时建议检查配置并查看启动日志：

```bash
docker compose config
docker compose up -d
docker compose logs -f
```

## GitHub Actions 发布 Docker Hub

仓库根目录内置 `.github/workflows/docker-publish.yml`：

- 推送到 `master`：构建 `ovh-bot/Dockerfile` 并推送 Docker Hub。
- 手动运行 workflow：同样构建并推送 Docker Hub。
- 推送 `v*` 标签：额外推送对应版本 tag。

GitHub 仓库需要配置 Secrets：

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`

Docker Hub Token 请在 Docker Hub 的账号安全设置里创建 Access Token，不要把账号密码写进 GitHub。

## 依赖维护

- `python-telegram-bot` 和 `ovh` 先保持固定版本，确认 API 兼容后再升级。
- `requests` 保持在下一个大版本之前。
- 每次升级依赖后，都重新运行 `py_compile`，并用 `/check` 做一次不下单的验证。
