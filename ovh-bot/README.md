# OVH 抢购 Bot v2

通过 Telegram Bot 自动抢购 OVH 服务器，锁定订单后手动付款。

## ✨ 功能特性

- 🌐 **支持 IE/EU/CA/US 所有区域**（IE 区价格最优）
- 📦 **指定存储/内存下单** — 不会再下成错误的 HDD/NVMe 配置
- 📡 **内置监控** — 有货自动下单，可设下单次数，到量自动停
- 🔍 **全配置检测** — 修复旧脚本只看第一个配置的 BUG
- 📊 **价格显示** — `/check` 有货配置实时查价
- 🏷️ **友好名称** — `/buy ks-1-b` 后通过按钮选择配置和机房
- 💬 **转发即下单** — 直接转发 OVH 到货信息自动识别下单
- 🔒 **默认拒绝匿名操作** — 未配置 Telegram 用户白名单时 Bot 拒绝启动

## 🚀 Docker 部署（推荐）

### 方式 1：docker-compose

```bash
# 1. 克隆仓库
git clone https://github.com/iumuu/1.git ovh-bot
cd ovh-bot/ovh-bot

# 2. 创建环境变量文件
cat > .env << 'EOF'
OVH_APPLICATION_KEY=你的Application_Key
OVH_APPLICATION_SECRET=你的Application_Secret
OVH_CONSUMER_KEY=你的Consumer_Key
OVH_ZONE=IE
TG_BOT_TOKEN=你的Bot_Token
TG_ALLOWED_USERS=你的TG用户ID
TG_ALLOW_ALL_USERS=false
TG_CHAT_ID=你的TG_Chat_ID
MONITOR_AUTO_BUY=false
MONITOR_MAX_ORDERS=1
EOF

# 3. 构建并启动
docker compose up -d --build

# 4. 查看日志
docker compose logs -f
```

### 方式 2：docker run

```bash
# 1. 克隆并构建
git clone https://github.com/iumuu/1.git ovh-bot
cd ovh-bot/ovh-bot
docker build -t ovh-bot .

# 2. 运行
docker run -d --name ovh-bot --restart unless-stopped \
  -e OVH_ENDPOINT=ovh-eu \
  -e OVH_APPLICATION_KEY=你的AK \
  -e OVH_APPLICATION_SECRET=你的AS \
  -e OVH_CONSUMER_KEY=你的CK \
  -e OVH_ZONE=IE \
  -e TG_BOT_TOKEN=你的Bot_Token \
  -e TG_ALLOWED_USERS=你的TG用户ID \
  -e TG_CHAT_ID=你的TG_Chat_ID \
  ovh-bot

# 3. 查看日志
docker logs -f ovh-bot
```

看到这行说明启动成功：
```
🤖 OVH 抢购 Bot v2 启动 (区域: IE/IE)
```

### 方式 3：配置文件

```bash
git clone https://github.com/iumuu/1.git ovh-bot
cd ovh-bot/ovh-bot
cp config.example.toml config.toml
# 编辑 config.toml 填入凭证

docker run -d --name ovh-bot --restart unless-stopped \
  -v $(pwd)/config.toml:/app/data/config.toml:ro \
  ovh-bot
```

### Docker 常用管理命令

```bash
docker logs -f ovh-bot       # 实时看日志
docker restart ovh-bot       # 重启
docker stop ovh-bot          # 停止
docker rm -f ovh-bot         # 删除容器
docker compose down          # 停止（compose 方式）
docker compose up -d --build # 更新代码后重新构建
```

### 运行监控模式（Docker）

```bash
docker run -d --name ovh-monitor --restart unless-stopped \
  -e OVH_ENDPOINT=ovh-eu \
  -e OVH_APPLICATION_KEY=你的AK \
  -e OVH_APPLICATION_SECRET=你的AS \
  -e OVH_CONSUMER_KEY=你的CK \
  -e OVH_ZONE=IE \
  -e TG_BOT_TOKEN=你的Bot_Token \
  -e TG_ALLOWED_USERS=你的TG用户ID \
  -e TG_CHAT_ID=你的TG_Chat_ID \
  ovh-bot python3 monitor.py ks-1-b ks-stor
```

## 📦 本地运行（不用 Docker）

### 前置要求

- Python 3.10+
- pip

### 安装依赖

```bash
cd ovh-bot
pip install -r requirements.txt
```

### 配置

```bash
cp config.example.toml config.toml
```

编辑 `config.toml`：

```toml
[ovh]
endpoint = "ovh-eu"        # IE 区也用 ovh-eu
application_key = "你的AK"
application_secret = "你的AS"
consumer_key = "你的CK"
zone = "IE"                # 关键！决定下单区域

[telegram]
bot_token = "你的Bot_Token"
allowed_users = [你的TG用户ID]
allow_all_users = false
chat_id = "你的TG_Chat_ID"

[defaults]
reinstall_os = "debian12_64"
# 留空时，一键安装使用 OVH 账号中的第一个 SSH 密钥
ssh_key = ""
```

`allowed_users` 必须至少配置一个用户。只有明确需要公开 Bot 时，才设置
`TG_ALLOW_ALL_USERS=true` 或 `allow_all_users = true`；公开 Bot 能触发下单，风险很高。

### 启动

```bash
# Telegram Bot 模式（推荐）
python3 bot.py

# CLI 模式
python3 bot.py check ks-1-b
python3 bot.py buy ks-1-b --dc fra

# 监控模式
python3 monitor.py ks-1-b ks-stor
```

Telegram Bot 的 `/watch` 会用按钮选择“自动下单（默认）”或“仅通知”，不需要设置
环境变量；创建后也可在 `/watchlist` 中随时切换。旧版已保存的监控任务继续按自动
下单运行。独立 `monitor.py` 仍使用 `[monitor]` 配置，和 Telegram 按钮互不影响。

## 🔑 获取凭证

### OVH API 凭证

访问 https://api.ovh.com/createToken/

| 字段 | 填写 |
|------|------|
| Validity | Unlimited |
| Rights | `GET /*` `POST /*` `PUT /*` `DELETE /*` |

记下 Application Key / Application Secret / Consumer Key

### Telegram Bot

1. @BotFather → `/newbot` → 获取 Bot Token
2. @userinfobot → 获取你的 User ID

## 📖 命令说明

### 🛒 下单类

| 命令 | 说明 |
|------|------|
| `/buy ks-1-b` | 查询 KS-1-B，并通过按钮选择硬件、机房和数量 |
| `/buy ks-stor` | 查询 KS-STOR 可抢配置 |
| `/buy ks-2` | 查询 KS-2 可抢配置 |
| `/check ks-1-b` | 查看所有配置可用性+价格 |
| `/catalog` | 查看服务器目录 |

### 📡 监控类

| 命令 | 说明 |
|------|------|
| `/watch ks-1-b` | 通过按钮选择配置、机房、下单上限和自动下单/仅通知模式 |
| `/watch ks-stor` | 设置 KS-STOR 监控 |
| `/unwatch ks-1-b` | 取消监控 |
| `/unwatch` | 取消所有监控 |
| `/watchlist` | 查看当前监控列表 |

### 💳 订单类

| 命令 | 说明 |
|------|------|
| `/pay 123456789` | 获取订单付款链接 |
| `/status 123456789` | 查看订单状态 |

### 🖥️ 服务器管理

`/servers` 会按正文长度和服务器数量自动分页，避免 Telegram 的
`Message_too_long` 错误；服务器名称、系统、机房和 IP 均使用代码格式，IP 可直接复制。

- `/servers` 只显示 OVH 返回有效磁盘组的服务器；退款后被暂停或等待删机的无磁盘服务会隐藏。
- 先在列表中选择服务器，页面会立即打开；进入该服务器的安装页面后才显示“一键安装”。
- 系统菜单不依赖容易阻塞的 `compatibleTemplates` 接口；SSH 密钥和安装请求有超时提示及重试按钮。
- “一键安装”预设为 `debian12_64`、默认 OVH SSH 密钥和默认磁盘组 RAID0。
- 一键安装仍需二次确认；缺少模板、密钥或同组至少两块磁盘时会拒绝提交。
- 手动安装盘选择会明确标注 NVMe SSD、SSD、HDD、容量、数量和 `diskGroupId`。
- RAID0 始终只使用一个磁盘组，不会把 SSD 和 HDD 混合组阵列。
- 安装进度全程显示 IP；安装完成后可点击“标记没中”。备注保存在
  `data/server_notes.json`，以后执行 `/servers` 会继续显示。

### 存储关键词

| 关键词 | 匹配 |
|--------|------|
| `nvme` | 所有 NVMe |
| `hdd` | 所有 HDD/SAS |
| `2x500nvme` | 精确 2x500GB NVMe |
| `2x960nvme` | 精确 2x960GB NVMe |
| `2x4hdd` | 精确 2x4TB HDD |

### 支持的服务器名称

| 名称 | planCode | CPU |
|------|----------|-----|
| `ks-1-b` | 26sk10b-v1 | Intel Xeon D-2123IT |
| `ks-stor` | 24skstor012-v1 | Intel Xeon-D 1521 |
| `ks-2` | 24sk202 | Intel Xeon-D 1540 |
| `ks-3` | 24sk302 | Intel Xeon-E3 1245 v5 |
| `ks-5` | 24sk502 | Intel Xeon-E3 1270 v6 |
| `rise-2` | 24rise02-v1 | Intel Xeon-E 2388G |
| `rise-5` | 24rise05-v1 | AMD Epyc 7413 |
| `advance-1` | 24adv01-v3 | AMD EPYC 4244P |
| ... | ... | ... |

> 也可以直接用 planCode，如 `/buy 26sk10b-v1`，再通过按钮选择配置。

## 🔧 修复的 BUG（vs coolci/OVH）

1. **有货显示无货** — 旧脚本只取 `availabilities[0]`，NVMe 版永远看不到
2. **不自动下单** — 监控器只通知不执行下单
3. **只能监控 HDD** — 同上，状态 key 没有区分 fqn
4. **下错配置** — 现在可以指定存储类型，不会把 NVMe 下成 HDD
5. **IE 区支持** — `endpoint: ovh-eu` + `zone: IE`

## 📁 项目结构

```
ovh-bot/
├── bot.py                 # 主脚本（TG Bot + CLI）
├── monitor.py             # 独立监控脚本
├── Dockerfile             # Docker 镜像
├── docker-compose.yml     # Docker Compose
├── requirements.txt       # Python 依赖
├── config.example.toml    # 配置模板
└── README.md              # 本文件
```

## 📄 License

MIT
