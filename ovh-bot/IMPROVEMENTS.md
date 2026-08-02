# 本次完善内容

## 下单安全

- Telegram 用户白名单改为默认拒绝：`allowed_users` 为空时 Bot 不启动。
- 如确需公开访问，必须显式设置 `TG_ALLOW_ALL_USERS=true`。
- 确认按钮采用一次性领取，避免 Telegram 重复回调触发重复下单。
- 批量下单会真实执行所选数量（最多 10 单），首次失败后立即停止。
- OVH 结账响应缺少 `orderId` 时不再误报成功。
- 独立监控器默认只通知；自动下单默认上限为 1 单。

## 正确性与可靠性

- 独立监控器把当前有货的内存、硬盘和机房精确传给下单函数，避免买错配置。
- 自动下单失败后进入冷却，库存持续可用时会在冷却结束后重试。
- Bot 中的 OVH 阻塞请求移入工作线程，下单操作使用锁串行化。
- 监控任务使用临时文件和原子替换持久化，降低断电或崩溃造成 JSON 损坏的概率。
- 本地运行的数据目录默认改为脚本旁的 `data/`；Docker 仍使用 `/app/data`。
- 配置改用标准 TOML 解析器，正确支持转义、数组、行内注释和字符串中的 `#`。

## 配置变化

- 新增 `TG_ALLOW_ALL_USERS`。
- 新增 `MONITOR_AUTO_BUY`、`MONITOR_MAX_ORDERS`、`MONITOR_INTERVAL`、
  `MONITOR_ORDER_COOLDOWN`、`MONITOR_DATACENTER`。
- `[monitor].auto_buy` 示例默认值由 `true` 改为 `false`。
- 新增 `[monitor].max_orders` 和 `[monitor].order_cooldown`。
- 新增 `[defaults].reinstall_os` 和 `[defaults].ssh_key`，用于 `/servers` 一键安装。

## 服务器安装体验

- `/servers` 的 IP 改为可直接复制的代码格式。
- 安装进度从提交开始就显示 IP，不再只在完成后显示。
- 安装完成后提供“标记没中”按钮，备注原子保存到 `data/server_notes.json`。
- 下次 `/servers` 会显示“没中”备注，并提供清除按钮。
- 新增一键安装预设：Debian 12、默认 SSH 密钥、默认单一磁盘组 RAID0。
- SSD、NVMe SSD 与 HDD 独立标识；RAID0 参数在 UI 和 API 层都禁止跨组混盘。

## 验证

```bash
python -m unittest discover -s tests -v
python -m py_compile bot.py monitor.py tests/test_ovh_bot.py
```

测试使用模拟客户端，不连接 OVH 或 Telegram，不会创建订单。
