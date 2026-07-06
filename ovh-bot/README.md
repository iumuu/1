# OVH 抢购 Bot v2

通过 Telegram Bot 自动抢购 OVH 服务器，锁定订单后手动付款。

## ✨ 功能特性

- 🌐 **支持 IE/EU/CA/US 所有区域**（IE 区价格最优）
- 📦 **指定存储/内存下单** — 不会再下成错误的 HDD/NVMe 配置
- 📡 **内置监控** — 有货自动下单，可设下单次数，到量自动停
- 🔍 **全配置检测** — 修复旧脚本只看第一个配置的 BUG
- 📊 **价格显示** — `/check` 有货配置实时查价
- 🏷️ **友好名称** — `/buy ks-1-b fra nvme` 代替难记的 planCode
- 💬 **转发即下单** — 直接转发 OVH 到货信息自动识别下单

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
TG_CHAT_ID=你的TG_Chat_ID
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
chat_id = "你的TG_Chat_ID"
```

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
| `/buy ks-1-b fra nvme` | 抢购 KS-1-B 法兰克福 NVMe 版 |
| `/buy ks-1-b fra hdd` | 抢购 KS-1-B 法兰克福 HDD 版 |
| `/buy ks-1-b fra 2x500nvme` | 精确指定 2x500GB NVMe |
| `/buy ks-stor lon` | 抢购 KS-STOR 伦敦 |
| `/buy ks-2` | 不限机房，有货就下 |
| `/check ks-1-b` | 查看所有配置可用性+价格 |
| `/catalog` | 查看服务器目录 |

### 📡 监控类

| 命令 | 说明 |
|------|------|
| `/watch ks-1-b fra nvme` | 监控 KS-1-B 法兰克福 NVMe，下1单后自动停 |
| `/watch ks-1-b nvme 2` | 监控 KS-1-B NVMe，下2单后自动停 |
| `/watch ks-stor lon 1` | 监控 KS-STOR 伦敦，下1单后自动停 |
| `/unwatch ks-1-b` | 取消监控 |
| `/unwatch` | 取消所有监控 |
| `/watchlist` | 查看当前监控列表 |

### 💳 订单类

| 命令 | 说明 |
|------|------|
| `/pay 123456789` | 获取订单付款链接 |
| `/status 123456789` | 查看订单状态 |

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

> 也可以直接用 planCode，如 `/buy 26sk10b-v1 fra nvme`

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
