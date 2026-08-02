#!/usr/bin/env python3
"""
OVH 服务器可用性监控脚本 v2
- 支持多配置组合同时监控（NVMe/HDD 等）
- 有货时通过 Telegram 通知 + 内联按钮一键下单
- 修复了旧脚本只能监控第一个配置的 bug

用法:
  python3 monitor.py [planCode1 planCode2 ...]

也可在 config.toml 的 [monitor] 部分配置
"""

import json
import logging
import os
import re
import sys
import time
import traceback
from pathlib import Path
from datetime import datetime, timedelta

import requests

sys.path.insert(0, str(Path(__file__).parent))
from bot import (
    OVHClient, load_config, parse_plan_code, parse_datacenter,
    guess_server_type, format_memory, format_storage,
    DC_DISPLAY_MAP, UNAVAILABLE_STATES, _parse_bool,
)

logger = logging.getLogger("ovh-monitor")
CONFIG_PATH = Path(__file__).parent / "config.toml"


class AvailabilityMonitor:
    """OVH 服务器可用性监控 v2 - 多配置支持"""

    def __init__(self, cfg: dict):
        self.client = OVHClient(cfg)
        self.tg_token = cfg.get("telegram", {}).get("bot_token", "")
        self.chat_id = str(cfg.get("telegram", {}).get("chat_id", "") or
                           os.environ.get("TG_CHAT_ID", ""))
        monitor_cfg = cfg.get("monitor", {})
        self.interval = max(5, int(monitor_cfg.get("interval", 10)))
        self.watch_list = [str(item).strip() for item in monitor_cfg.get("watch_list", []) if str(item).strip()]
        self.auto_buy = _parse_bool(monitor_cfg.get("auto_buy", False))
        self.max_orders = max(1, min(int(monitor_cfg.get("max_orders", 1)), 100))
        self.orders_placed = 0
        self.default_dc = (cfg.get("monitor", {}).get("datacenter") or
                           cfg.get("defaults", {}).get("datacenter", "")).lower()

        # 状态跟踪: key = "planCode|dc|fqn", value = status
        # 这样每种配置组合在同一个数据中心都能独立追踪
        self.last_status = {}
        # 防重复尝试: key 同上, value = timestamp。失败也进入冷却，避免刷 API。
        self.recent_attempts = {}
        self.order_cooldown = max(30, int(monitor_cfg.get("order_cooldown", 120)))

    def send_telegram(self, text: str, reply_markup=None):
        """发送 Telegram 消息"""
        if not self.tg_token or not self.chat_id:
            logger.warning("未配置 Telegram 通知")
            return False

        url = f"https://api.telegram.org/bot{self.tg_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)

        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code != 200:
                logger.error(f"Telegram 发送失败: {resp.text[:200]}")
                return False
            return True
        except Exception as e:
            logger.error(f"Telegram 发送异常: {e}")
            return False

    def _now_str(self) -> str:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")

    def _status_key(self, plan_code: str, dc: str, fqn: str) -> str:
        """生成状态追踪的 key（包含 fqn 以区分不同配置）"""
        return f"{plan_code}|{dc}|{fqn}"

    def check_and_notify(self):
        """检查所有监控服务器的可用性"""
        for plan_code in self.watch_list:
            try:
                self._check_one(plan_code)
            except Exception as e:
                logger.error(f"检查 {plan_code} 出错: {e}\n{traceback.format_exc()}")

    def _check_one(self, plan_code: str):
        """检查单个服务器的可用性"""
        logger.info(f"🔍 检查 {plan_code} 所有配置...")

        all_configs = self.client.check_availability(plan_code)
        if not all_configs:
            logger.warning(f"未获取到 {plan_code} 的可用性数据")
            return

        for cfg in all_configs:
            fqn = cfg["fqn"]
            memory = cfg["memory"]
            storage = cfg["storage"]
            mem_display = format_memory(memory)
            stor_display = format_storage(storage)

            for dc, status in cfg["datacenters"].items():
                # 如果用户指定了默认数据中心，只监控那个
                if self.default_dc and dc != self.default_dc:
                    continue

                key = self._status_key(plan_code, dc, fqn)
                old_status = self.last_status.get(key)

                # 更新当前状态
                self.last_status[key] = status

                # 判断状态变化
                if status in UNAVAILABLE_STATES:
                    continue  # 无货，不通知

                # 有货的情况
                is_new = old_status is None
                became_available = (old_status in UNAVAILABLE_STATES)
                remains_available = not is_new and old_status not in UNAVAILABLE_STATES
                should_notify = is_new or became_available
                should_attempt = self.auto_buy and (should_notify or remains_available)

                if not should_notify and not should_attempt:
                    continue

                now = time.time()
                last_attempt = self.recent_attempts.get(key)
                if should_attempt and last_attempt is not None:
                    elapsed = now - last_attempt
                    if elapsed < self.order_cooldown:
                        logger.info(f"跳过 {key}，下次尝试还需 {self.order_cooldown - int(elapsed)} 秒")
                        continue

                if self.auto_buy and self.orders_placed >= self.max_orders:
                    if should_notify:
                        logger.info(f"{key} 有货，但已达到下单上限 {self.max_orders}")
                    continue

                reason = (
                    "首次检查发现" if is_new
                    else "从无货变为有货" if became_available
                    else "持续有货，冷却结束后重试"
                )
                if should_notify:
                    logger.info(f"🔥 {plan_code} {mem_display}+{stor_display} @ {dc}: {reason}")

                dc_display = DC_DISPLAY_MAP.get(dc, dc)
                text = (
                    f"🔥 *服务器有货！*\n\n"
                    f"📦 服务器: `{plan_code}`\n"
                    f"💾 内存: {mem_display}\n"
                    f"💿 存储: {stor_display}\n"
                    f"📍 数据中心: {dc_display}\n"
                    f"📊 状态: {status}\n"
                    f"🕐 时间: {self._now_str()}\n"
                )

                if self.auto_buy:
                    text += f"\n🚀 正在自动下单... ({self.orders_placed + 1}/{self.max_orders})"
                    self.send_telegram(text)
                    self.recent_attempts[key] = now

                    # 精确传入当前有货配置，避免下成同机房的其它硬件组合。
                    server_type = guess_server_type(plan_code)
                    result = self.client.quick_buy(
                        plan_code=plan_code,
                        server_type=server_type,
                        datacenter=dc,
                        target_memory=memory,
                        target_storage=storage,
                    )

                    if result["success"]:
                        self.orders_placed += 1
                        buy_text = (
                            f"✅ *自动抢购成功！*\n\n"
                            f"📦 服务器: `{plan_code}`\n"
                            f"💾 内存: {mem_display}\n"
                            f"💿 存储: {stor_display}\n"
                            f"📍 数据中心: {dc_display}\n"
                            f"🛒 购物车: `{result['cart_id']}`\n"
                        )
                        if result["order_id"]:
                            buy_text += f"📋 订单号: `{result['order_id']}`\n"
                        if result["payment_url"]:
                            buy_text += f"💳 付款链接: {result['payment_url']}\n"
                        if result.get("price"):
                            p = result["price"]
                            buy_text += f"💰 价格: {p.get('withTax', '?')} {p.get('currencyCode', 'EUR')}\n"
                        buy_text += f"\n📊 下单进度: {self.orders_placed}/{self.max_orders}"
                        buy_text += f"\n⏱️ 耗时: {result['elapsed']}s"
                        if result["order_id"]:
                            buy_text += "\n\n⚠️ *请尽快手动付款！*"
                    else:
                        buy_text = (
                            f"❌ *自动抢购失败*\n\n"
                            f"📦 服务器: `{plan_code}`\n"
                            f"📍 数据中心: {dc_display}\n"
                            f"❗ 错误: {result['error']}\n"
                            f"⏳ {self.order_cooldown} 秒后仍有货将重试\n"
                        )

                    self.send_telegram(buy_text)
                elif should_notify:
                    # 独立 monitor.py 不接收 Telegram 回调，因此不发送无效按钮。
                    text += f"\n💡 请在 Bot 中执行 `/buy {plan_code}` 选择配置下单。"
                    self.send_telegram(text)

    def run(self):
        """启动监控循环"""
        if not self.watch_list:
            raise ValueError("监控列表为空，请通过参数或 [monitor].watch_list 指定 planCode")
        logger.info(f"🚀 OVH 可用性监控 v2 启动")
        logger.info(f"   监控服务器: {self.watch_list or '全部'}")
        logger.info(f"   默认数据中心: {self.default_dc or '全部'}")
        logger.info(f"   检查间隔: {self.interval}s")
        logger.info(f"   自动下单: {'是' if self.auto_buy else '否'}")
        if self.auto_buy:
            logger.info(f"   下单上限: {self.max_orders}")
        logger.info(f"   区域: {self.client.zone}/{self.client.subsidiary}")

        while True:
            try:
                self.check_and_notify()
            except Exception as e:
                logger.error(f"监控循环出错: {e}")

            time.sleep(self.interval)


if __name__ == "__main__":
    cfg = load_config()

    # 支持命令行参数指定监控的服务器
    watch_list = sys.argv[1:]
    if watch_list:
        if "monitor" not in cfg:
            cfg["monitor"] = {}
        cfg["monitor"]["watch_list"] = watch_list

    try:
        monitor = AvailabilityMonitor(cfg)
        monitor.run()
    except KeyboardInterrupt:
        logger.info("收到停止信号，监控已退出")
    except ValueError as e:
        logger.error(str(e))
        sys.exit(2)
