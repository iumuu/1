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
    DC_DISPLAY_MAP, UNAVAILABLE_STATES,
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
        self.interval = cfg.get("monitor", {}).get("interval", 10)
        self.watch_list = cfg.get("monitor", {}).get("watch_list", [])
        self.auto_buy = cfg.get("monitor", {}).get("auto_buy", True)
        self.default_dc = (cfg.get("monitor", {}).get("datacenter") or
                           cfg.get("defaults", {}).get("datacenter", ""))

        # 状态跟踪: key = "planCode|dc|fqn", value = status
        # 这样每种配置组合在同一个数据中心都能独立追踪
        self.last_status = {}
        # 防重复下单: key 同上, value = timestamp
        self.recently_ordered = {}
        self.order_cooldown = 120  # 2 分钟防重复

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
                is_available = not is_new and old_status not in UNAVAILABLE_STATES

                if is_new or became_available:
                    # 检查是否最近刚下过单
                    now = time.time()
                    if key in self.recently_ordered:
                        elapsed = now - self.recently_ordered[key]
                        if elapsed < self.order_cooldown:
                            logger.info(f"跳过 {key}，2分钟内已下单")
                            continue

                    reason = "首次检查发现" if is_new else "从无货变为有货"
                    logger.info(f"🔥 {plan_code} {mem_display}+{stor_display} @ {dc}: {reason}")

                    # 构建 Telegram 消息
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
                        text += "\n🚀 正在自动下单..."
                        self.send_telegram(text)

                        # 自动抢购
                        server_type = guess_server_type(plan_code)
                        result = self.client.quick_buy(
                            plan_code=plan_code,
                            server_type=server_type,
                            datacenter=dc,
                        )

                        if result["success"]:
                            self.recently_ordered[key] = time.time()
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
                            buy_text += f"\n⏱️ 耗时: {result['elapsed']}s"
                            if result["order_id"]:
                                buy_text += "\n\n⚠️ *请尽快手动付款！*"
                        else:
                            buy_text = (
                                f"❌ *自动抢购失败*\n\n"
                                f"📦 服务器: `{plan_code}`\n"
                                f"📍 数据中心: {dc_display}\n"
                                f"❗ 错误: {result['error']}\n"
                            )

                        self.send_telegram(buy_text)
                    else:
                        # 不自动下单，只通知并提供按钮
                        buttons = {
                            "inline_keyboard": [[
                                {"text": f"🛒 抢购 {mem_display}+{stor_display} @{dc}",
                                 "callback_data": f"buy|{plan_code}|{dc}"}
                            ]]
                        }
                        self.send_telegram(text, reply_markup=buttons)

    def run(self):
        """启动监控循环"""
        logger.info(f"🚀 OVH 可用性监控 v2 启动")
        logger.info(f"   监控服务器: {self.watch_list or '全部'}")
        logger.info(f"   默认数据中心: {self.default_dc or '全部'}")
        logger.info(f"   检查间隔: {self.interval}s")
        logger.info(f"   自动下单: {'是' if self.auto_buy else '否'}")
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

    monitor = AvailabilityMonitor(cfg)
    monitor.run()
