# OVH 抢购 Bot v2

通过 Telegram Bot 自动抢购 OVH 服务器，锁定订单后手动付款。

## ⚡ v2 更新重点

- ✅ **支持 IE 区**（爱尔兰区，价格最优）- 通过 `zone = "IE"` 配置
- ✅ **修复可用性检测 BUG** - 显示所有配置组合（NVMe/HDD 等），不再只返回第一个
- ✅ **修复自动下单** - 完整处理 requiredConfiguration + eco/options
- ✅ **多配置同时监控** - KS-1 的 NVMe 版本和 HDD 版本都能监控到
- ✅ **内联按钮** - `/check` 显示有货时可以直接点按钮下单

## 🔑 关键概念

| 术语 | 说明 |
|------|------|
| `endpoint` | API 入口：`ovh-eu` (欧/IE共用) / `ovh-ca` / `ovh-us` |
| `zone` | 下单区域 = `ovhSubsidiary`：IE/FR/DE/CA/US 等 |
| `planCode` | 服务器型号代码：24ska01, ks-a, rise-2 等 |
| `fqn` | 完整配置名：区分 NVMe/HDD 等不同配置组合 |

**IE 区说明**：IE 区（爱尔兰）使用 `endpoint: ovh-eu` + `zone: IE`，API 入口一样但 `ovhSubsidiary` 不同，决定下单时的区域和价格。

## 快速开始

### 1. 获取 OVH API 凭证

访问 https://api.ovh.com/createToken/ 创建 API 应用：
- Rights: `GET /*` 和 `POST /*`
- 记下 Application Key / Secret / Consumer Key

### 2. 创建 Telegram Bot

1. @BotFather → `/newbot` → 获取 Token
2. @userinfobot → 获取你的 User ID

### 3. 配置

```bash
cp config.example.toml config.toml
```

编辑 config.toml，**关键配置**：

```toml
[ovh]
endpoint = "ovh-eu"        # IE 区也用 ovh-eu
zone = "IE"                # ⭐ 这是关键！设置为 IE 才能下 IE 区的订单

[telegram]
bot_token = "your_token"
allowed_users = [123456789]
chat_id = "123456789"      # 监控通知用
```

### 4. 运行

```bash
# 启动 Telegram Bot
python3 bot.py

# CLI 直接抢购
python3 bot.py buy 24ska01 --dc fra

# 查看服务器所有配置的可用性
python3 bot.py check ks-a

# 启动监控（有货自动下单）
python3 monitor.py 24ska01 24sklea01
```

## Telegram Bot 命令

| 命令 | 说明 |
|------|------|
| `/buy 24ska01 fra` | 抢购服务器（自动选有货的配置） |
| `/check ks-a` | 查看 **所有配置**（NVMe/HDD）的可用性 |
| `/catalog eco` | 查看服务器目录 |
| `/pay 123456` | 获取付款链接 |
| `/status 123456` | 订单状态 |

## 🔧 修复的 BUG（vs coolci/OVH）

### 1. 有货显示无货
**原因**：旧脚本 `check_server_availability()` 无配置时默认取 `availabilities[0]`，永远是第一个配置（通常是 HDD 版本），其他配置（如 NVMe）被忽略。

**修复**：`/check` 命令返回 **所有** 配置组合，`quick_buy()` 自动遍历找有货的。

### 2. 不自动下单
**原因**：旧脚本监控器检测到状态变化后只通知，没有调用 `purchase_server()`。

**修复**：监控模式默认 `auto_buy = true`，检测到有货直接调用 `quick_buy()`。

### 3. 只能监控 2x2TB HDD
**原因**：同 BUG 1，默认取第一个配置。KS-1 有多个 FQN：
- `ks-a.ram-32g-ecc-2400.softraid-2x2000sa` (2x2TB HDD)
- `ks-a.ram-32g-ecc-2400.softraid-2x450nvme` (2x450GB NVMe)

**修复**：状态追踪 key 包含 `fqn`，每种配置独立追踪。

## 数据中心代码

| 代码 | 位置 | 区域 |
|------|------|------|
| bhs | 🇨🇦 Beauharnois | 加拿大 |
| gra | 🇫🇷 Gravelines | 欧洲 |
| sbg | 🇫🇷 Strasbourg | 欧洲 |
| rbx | 🇫🇷 Roubaix | 欧洲 |
| fra | 🇩🇪 Frankfurt | 欧洲 |
| par | 🇫🇷 Paris | 欧洲 |
| lon | 🇬🇧 London | 欧洲 |
| sgp | 🇸🇬 Singapore | 亚太 |
