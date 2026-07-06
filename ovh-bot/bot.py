#!/usr/bin/env python3
"""
OVH 服务器抢购脚本 v2
- 支持 IE/EU/CA/US 所有区域
- 支持多配置组合（NVMe/HDD 等）同时监控
- 通过 Telegram Bot 接收服务器信息自动下单锁定
- 自动处理 requiredConfiguration / eco/options

用法:
  1. 复制 config.example.toml 为 config.toml 并填入配置
  2. python3 bot.py

Telegram 命令:
  /buy <planCode> [datacenter] [os]       - 立即抢购
  /check <planCode>                        - 检查服务器所有配置的可用性
  /catalog [category]                      - 查看服务器目录
  /pay <orderId>                           - 获取付款链接
  /help                                    - 帮助信息

也可以直接转发 OVH 的服务器信息，Bot 会自动解析并下单。
"""

import json
import logging
import os
import re
import sys
import time
import traceback
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta

# 北京时区 (UTC+8)
BJT = timezone(timedelta(hours=8))


def to_bjt(dt_str: str) -> str:
    """将 OVH 返回的时间字符串转换为北京时间可读格式"""
    if not dt_str or dt_str == "N/A":
        return "N/A"
    try:
        # OVH 格式: 2026-07-04T09:24:47+02:00
        dt = datetime.fromisoformat(dt_str)
        dt_bjt = dt.astimezone(BJT)
        return dt_bjt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return dt_str

import requests
import ovh

# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ovh-bot")

# ============================================================
# 配置加载
# ============================================================
# 配置文件路径（Docker 挂载目录优先）
CONFIG_PATHS = [
    Path("/app/data/config.toml"),   # Docker 挂载
    Path(__file__).parent / "config.toml",  # 本地开发
]


def parse_toml_simple(path: str) -> dict:
    """简易 TOML 解析器"""
    config = {}
    current_section = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = re.match(r'^\[(\w+)\]$', line)
            if m:
                current_section = m.group(1)
                config[current_section] = {}
                continue
            m = re.match(r'^(\w+)\s*=\s*(.+)$', line)
            if m:
                key = m.group(1)
                val = m.group(2).strip()
                if ' #' in val:
                    val = val[:val.index(' #')].strip()
                if val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                elif val.isdigit():
                    val = int(val)
                elif val == "true":
                    val = True
                elif val == "false":
                    val = False
                elif val.startswith("["):
                    inner = val[1:-1].strip()
                    if inner:
                        val = [v.strip().strip('"').strip("'") for v in inner.split(",")]
                    else:
                        val = []
                if current_section:
                    config[current_section][key] = val
                else:
                    config[key] = val
    return config


def load_config() -> dict:
    """加载配置，优先级: 环境变量 > config.toml > 默认值"""
    # 按优先级查找配置文件
    cfg = {}
    for cp in CONFIG_PATHS:
        if cp.exists():
            cfg = parse_toml_simple(str(cp))
            break

    # 环境变量映射（显式映射，避免 key 拆分错误）
    env_map = {
        "OVH_ENDPOINT":          ("ovh", "endpoint"),
        "OVH_APPLICATION_KEY":   ("ovh", "application_key"),
        "OVH_APPLICATION_SECRET":("ovh", "application_secret"),
        "OVH_CONSUMER_KEY":      ("ovh", "consumer_key"),
        "OVH_ZONE":              ("ovh", "zone"),
        "TG_BOT_TOKEN":          ("telegram", "bot_token"),
        "TG_CHAT_ID":            ("telegram", "chat_id"),
    }

    for env_key, (section, cfg_key) in env_map.items():
        val = os.environ.get(env_key, "")
        if val:
            if section not in cfg:
                cfg[section] = {}
            cfg[section][cfg_key] = val

    # TG_ALLOWED_USERS 单独处理（逗号分隔 → list[int]）
    users_str = os.environ.get("TG_ALLOWED_USERS", "")
    if users_str:
        if "telegram" not in cfg:
            cfg["telegram"] = {}
        cfg["telegram"]["allowed_users"] = [int(u.strip()) for u in users_str.split(",") if u.strip()]

    # config.toml 中的 allowed_users 格式修正
    if "telegram" in cfg and "allowed_users" in cfg["telegram"]:
        users = cfg["telegram"]["allowed_users"]
        if isinstance(users, str):
            cfg["telegram"]["allowed_users"] = [int(u.strip()) for u in users.split(",") if u.strip()]
        elif isinstance(users, list):
            cfg["telegram"]["allowed_users"] = [int(u) for u in users]

    # 默认值
    if "ovh" not in cfg:
        cfg["ovh"] = {}
    cfg["ovh"].setdefault("endpoint", "ovh-eu")
    cfg["ovh"].setdefault("zone", "IE")

    return cfg


# ============================================================
# 常量
# ============================================================
# 数据中心 → 区域映射
EU_DATACENTERS = {"gra", "rbx", "sbg", "eri", "lim", "waw", "par", "fra", "lon"}
CANADA_DATACENTERS = {"bhs"}
US_DATACENTERS = {"vin", "hil"}
APAC_DATACENTERS = {"syd", "sgp", "ynm"}

# Zone → ovhSubsidiary 映射
ZONE_MAP = {
    "IE": "IE", "FR": "FR", "DE": "DE", "UK": "UK",
    "PL": "PL", "ES": "ES", "IT": "IT", "PT": "PT",
    "NL": "NL", "CZ": "CZ", "FI": "FI", "LT": "LT",
    "CA": "CA", "US": "US", "AU": "AU", "SG": "SG",
    "IN": "IN",
}

# endpoint 映射
ENDPOINT_MAP = {
    "ovh-eu": "https://eu.api.ovh.com",
    "ovh-ca": "https://ca.api.ovh.com",
    "ovh-us": "https://api.us.ovhcloud.com",
}

# 可用性状态排除
UNAVAILABLE_STATES = {"unavailable", "unknown"}


def get_region_for_dc(dc: str) -> str:
    """根据数据中心代码推断区域"""
    dc_lower = dc.lower()
    if any(dc_lower.startswith(p) for p in EU_DATACENTERS):
        return "europe"
    elif any(dc_lower.startswith(p) for p in CANADA_DATACENTERS):
        return "canada"
    elif any(dc_lower.startswith(p) for p in US_DATACENTERS):
        return "usa"
    elif any(dc_lower.startswith(p) for p in APAC_DATACENTERS):
        return "apac"
    return ""


def format_storage(storage: str) -> str:
    """格式化存储显示"""
    if not storage or storage == "N/A":
        return "N/A"
    s = storage.lower()

    # 混合存储: hybridsoftraid-4x4000sa-1x500nvme → 4x4TB HDD + 1x500GB NVMe
    if "hybrid" in s:
        parts = []
        # 提取 HDD/SAS 部分
        sa_match = re.search(r'(\d+)x(\d+)sa', s)
        if sa_match:
            size = int(sa_match.group(2))
            unit = "TB" if size >= 1000 else "GB"
            val = size // 1000 if size >= 1000 else size
            parts.append(f"{sa_match.group(1)}x{val}{unit} HDD")
        # 提取 NVMe 部分
        nvme_match = re.search(r'(\d+)x(\d+)nvme', s)
        if nvme_match:
            parts.append(f"{nvme_match.group(1)}x{nvme_match.group(2)}GB NVMe")
        # 提取 SSD 部分
        ssd_match = re.search(r'(\d+)x(\d+)ssd', s)
        if ssd_match:
            parts.append(f"{ssd_match.group(1)}x{ssd_match.group(2)}GB SSD")
        return " + ".join(parts) if parts else storage

    # 纯 NVMe: softraid-2x500nvme → 2x500GB NVMe
    if "nvme" in s:
        m = re.search(r'(\d+)x(\d+)(nvme)', s)
        if m:
            return f"{m.group(1)}x{m.group(2)}GB NVMe"

    # 纯 HDD/SAS: softraid-2x2000sa → 2x2TB HDD
    if "sas" in s or "sa" in s:
        m = re.search(r'(\d+)x(\d+)', s)
        if m:
            size = int(m.group(2))
            unit = "TB" if size >= 1000 else "GB"
            val = size // 1000 if size >= 1000 else size
            return f"{m.group(1)}x{val}{unit} {'SAS' if 'sas' in s else 'HDD'}"

    # SSD: softraid-2x480ssd
    if "ssd" in s:
        m = re.search(r'(\d+)x(\d+)ssd', s)
        if m:
            return f"{m.group(1)}x{m.group(2)}GB SSD"

    return storage


def format_memory(memory: str) -> str:
    """格式化内存显示"""
    if not memory or memory == "N/A":
        return "N/A"
    m = re.search(r'ram-(\d+)', memory.lower())
    if m:
        size = int(m.group(1))
        unit = "GB" if size < 1000 else "TB"
        val = size if size < 1000 else size // 1000
        ecc = "ECC" if "ecc" in memory.lower() else ""
        return f"{val}{unit} {'DDR4' if 'ddr4' in memory.lower() else ''} {ecc}".strip()
    return memory


def storage_matches(storage_raw: str, target: str) -> bool:
    """检查存储配置是否匹配用户指定类型

    支持的 target:
      - None/"" → 不限制，匹配所有
      - "nvme" → 匹配任何 NVMe
      - "hdd" → 匹配任何 HDD/SAS
      - "2x500nvme" → 精确匹配 2x500...nvme
      - "2x4hdd" → 匹配 2x4000sa (2x4TB HDD)
      - "softraid-2x450nvme" → 完整 OVH 存储配置精确匹配
    """
    if not target or not storage_raw:
        return True

    raw = storage_raw.lower().replace(" ", "")
    tgt = target.lower().replace(" ", "")
    s = raw.replace("gb", "").replace("tb", "")
    t = tgt.replace("gb", "").replace("tb", "")

    # 完整 OVH storage 配置，走标准化精确匹配
    if "softraid" in tgt or "raid" in tgt:
        return OVHClient._standardize(raw) == OVHClient._standardize(tgt)

    # 简单类型匹配
    if t == "nvme":
        return "nvme" in s
    if t in ("hdd", "sas"):
        return ("nvme" not in s) and ("sa" in s)

    # 精确匹配: 2x500nvme → 在原始 storage 中查找
    if re.match(r'\d+x\d+nvme$', t):
        m = re.match(r'(\d+x\d+)nvme$', t)
        if m:
            prefix = m.group(1)
            return prefix in s and "nvme" in s
        return t in s

    # HDD/SATA 精确匹配: 2x4hdd / 2x4tb / 2x4000sa → 查找 2x4000sa
    hdd_m = re.match(r'^(\d+)x(\d+)(?:hdd|tb|tbs|sa)?$', t)
    if hdd_m and "nvme" not in t and "ssd" not in t:
        count = hdd_m.group(1)
        size_val = int(hdd_m.group(2))
        # 小于 100 视为 TB，转成 OVH SATA/SAS 的 GB 数字；大于等于 100 视为 GB
        sa_val = str(size_val * 1000 if size_val < 100 else size_val)
        return f"{count}x{sa_val}" in s and "sa" in s

    # 无法解析时不放行，避免 2x450nvme 错落到其它存储
    return OVHClient._standardize(raw) == OVHClient._standardize(tgt)


def memory_matches(memory_raw: str, target: str) -> bool:
    """检查内存配置是否匹配用户指定大小

    支持的 target:
      - None/"" → 不限制
      - "32g" / "32gb" → 匹配 32GB
      - "ram-32g-ecc-2133" → 精确解析并匹配 32GB
    """
    if not target or not memory_raw:
        return True

    raw = memory_raw.lower().replace(" ", "")
    tgt = target.lower().replace(" ", "")

    raw_m = re.search(r'ram-(\d+)', raw)
    if not raw_m:
        return False
    raw_size = int(raw_m.group(1))

    # 优先解析完整 OVH 内存配置，如 ram-32g-ecc-2133 / ram-64g-ecc-2400
    tgt_m = re.search(r'ram-(\d+)', tgt)
    if tgt_m:
        return raw_size == int(tgt_m.group(1))

    # 解析简写，如 32g / 32gb / 64
    simple_m = re.search(r'^(\d+)(?:g|gb)?$', tgt)
    if simple_m:
        return raw_size == int(simple_m.group(1))

    # 无法解析时不要放行，最多允许标准化后的精确包含匹配
    tgt_norm = tgt.replace("gb", "g")
    raw_norm = raw.replace("gb", "g")
    return tgt_norm in raw_norm


# ============================================================
# OVH API 客户端 v2
# ============================================================
class OVHClient:
    """OVH API 封装 - 完整支持多区域、多配置"""

    def __init__(self, cfg: dict):
        ovh_cfg = cfg.get("ovh", {})
        self.endpoint = ovh_cfg.get("endpoint", "ovh-eu")
        self.ak = ovh_cfg.get("application_key", "")
        self.as_ = ovh_cfg.get("application_secret", "")
        self.ck = ovh_cfg.get("consumer_key", "")
        self.zone = ovh_cfg.get("zone", "IE").upper()  # ovhSubsidiary

        if not all([self.ak, self.as_, self.ck]):
            logger.warning("OVH API 凭证不完整，部分功能不可用")

        self.client = ovh.Client(
            endpoint=self.endpoint,
            application_key=self.ak,
            application_secret=self.as_,
            consumer_key=self.ck,
        )

        self.defaults = cfg.get("defaults", {})

    @property
    def subsidiary(self) -> str:
        """获取 ovhSubsidiary"""
        return ZONE_MAP.get(self.zone, self.zone)

    def _call(self, method: str, path: str, **kwargs):
        """统一 API 调用 - 使用 ovh 库的便捷方法支持 kwargs"""
        try:
            m = method.upper()
            if m == "GET":
                return self.client.get(path, **kwargs)
            elif m == "POST":
                return self.client.post(path, **kwargs)
            elif m == "PUT":
                return self.client.put(path, **kwargs)
            elif m == "DELETE":
                return self.client.delete(path, **kwargs)
            else:
                raise ValueError(f"不支持的 HTTP 方法: {method}")
        except ovh.exceptions.APIError as e:
            logger.error(f"API 调用失败: {method} {path} -> {e}")
            raise

    def get(self, path, **kwargs):
        return self._call("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self._call("POST", path, **kwargs)

    def put(self, path, **kwargs):
        return self._call("PUT", path, **kwargs)

    def delete(self, path, **kwargs):
        return self._call("DELETE", path, **kwargs)

    # ---- 可用性检查 (修复版：返回所有配置) ----
    def check_availability(self, plan_code: str) -> list:
        """
        检查服务器所有配置组合的可用性

        返回 list of dict:
        [
            {
                "fqn": "24ska01.ram-32g-ecc-2400.softraid-2x450nvme",
                "memory": "ram-32g-ecc-2400",
                "storage": "softraid-2x450nvme",
                "datacenters": {"bhs": "unavailable", "gra": "available", ...},
            },
            ...
        ]
        """
        path = "/dedicated/server/datacenter/availabilities"
        try:
            availabilities = self.get(path, planCode=plan_code)
        except Exception as e:
            logger.error(f"可用性查询失败: {e}")
            return []

        if not availabilities:
            return []

        results = []
        for item in availabilities:
            memory = item.get("memory", "N/A")
            storage = item.get("storage", "N/A")
            fqn = item.get("fqn", "")

            dcs = {}
            for dc_info in item.get("datacenters", []):
                dc_name = dc_info.get("datacenter")
                avail = dc_info.get("availability", "unknown")
                if dc_name:
                    dcs[dc_name] = avail

            results.append({
                "fqn": fqn,
                "memory": memory,
                "storage": storage,
                "datacenters": dcs,
            })

        return results

    def find_available_configs(self, plan_code: str, target_dc: str = None,
                               target_storage: str = None,
                               target_memory: str = None) -> list:
        """找出所有有货且符合指定存储/内存配置的组合

        target_storage: "nvme" / "hdd" / "2x500nvme" / "2x4hdd" 等
        target_memory: "32g" / "64g" 等
        """
        all_configs = self.check_availability(plan_code)
        available = []

        for cfg in all_configs:
            if not storage_matches(cfg["storage"], target_storage):
                continue
            if not memory_matches(cfg["memory"], target_memory):
                continue

            for dc, status in cfg["datacenters"].items():
                if status in UNAVAILABLE_STATES:
                    continue
                if target_dc and dc != target_dc:
                    continue
                available.append({
                    "fqn": cfg["fqn"],
                    "memory": cfg["memory"],
                    "storage": cfg["storage"],
                    "datacenter": dc,
                    "availability": status,
                    "memory_display": format_memory(cfg["memory"]),
                    "storage_display": format_storage(cfg["storage"]),
                })

        return available

    def get_catalog(self, category: str = "eco") -> dict:
        """获取服务器目录"""
        path = f"/order/catalog/public/{category}"
        try:
            return self.get(path, ovhSubsidiary=self.subsidiary)
        except Exception as e:
            logger.error(f"获取目录失败: {e}")
            return {}

    def get_config_price(self, plan_code: str, datacenter: str,
                         memory: str, storage: str) -> str:
        """通过创建临时购物车获取指定配置的精确价格

        Returns: 价格字符串如 "€47.98" 或 ""
        """
        cart_id = None
        try:
            # 创建购物车
            cart = self.create_cart()
            cart_id = cart["cartId"]

            # 添加基础商品
            item_result = self.post(
                f"/order/cart/{cart_id}/eco",
                planCode=plan_code,
                duration="P1M",
                pricingMode="default",
                quantity=1,
            )
            item_id = item_result["itemId"]

            # 设置数据中心区域
            region = get_region_for_dc(datacenter)
            configurations = {
                "dedicated_datacenter": datacenter,
                "dedicated_os": "none_64.en",
            }
            if region:
                configurations["region"] = region

            for label, value in configurations.items():
                try:
                    self.post(
                        f"/order/cart/{cart_id}/item/{item_id}/configuration",
                        label=label,
                        value=str(value),
                    )
                except Exception:
                    pass

            # 添加硬件选项
            options = self._find_addon_options(plan_code, memory, storage)
            if options:
                try:
                    available_opts = self.get(
                        f"/order/cart/{cart_id}/eco/options",
                        planCode=plan_code,
                    )
                    for wanted in options:
                        for avail in available_opts:
                            if avail.get("planCode") == wanted:
                                try:
                                    self.post(
                                        f"/order/cart/{cart_id}/eco/options",
                                        itemId=item_id,
                                        planCode=wanted,
                                        duration=avail.get("duration", "P1M"),
                                        pricingMode=avail.get("pricingMode", "default"),
                                        quantity=1,
                                    )
                                except Exception:
                                    pass
                                break
                except Exception:
                    pass

            # 获取价格
            summary = self.get(f"/order/cart/{cart_id}/summary")
            prices = summary.get("prices", {})
            with_tax = prices.get("withTax", {})
            price_value = with_tax.get("value") if isinstance(with_tax, dict) else with_tax
            currency = with_tax.get("currencyCode", "EUR") if isinstance(with_tax, dict) else "EUR"

            if price_value is not None:
                # 转换为可读格式
                if isinstance(price_value, (int, float)):
                    if price_value > 100000:
                        price_value = price_value / 100000000  # OVH 返回的是纳单位
                    return f"{price_value:.2f} {currency}"
                return str(price_value)
            return ""
        except Exception as e:
            logger.warning(f"查价失败: {e}")
            return ""
        finally:
            if cart_id:
                try:
                    self.delete_cart(cart_id)
                except Exception:
                    pass

    def get_plan_addon_families(self, plan_code: str, category: str = "eco") -> list:
        """获取 planCode 的 addonFamilies（用于查找硬件选项）"""
        try:
            catalog = self.get_catalog(category)
            for plan in catalog.get("plans", []):
                if plan.get("planCode") == plan_code:
                    return plan.get("addonFamilies", [])
        except Exception:
            pass
        return []

    # ---- 下单流程（完整版） ----
    def create_cart(self) -> dict:
        """创建购物车"""
        path = "/order/cart"
        body = {
            "ovhSubsidiary": self.subsidiary,
            "description": f"ovh-bot-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        }
        result = self.post(path, **body)
        logger.info(f"购物车已创建: {result.get('cartId')}")
        return result

    def add_eco_server(self, cart_id: str, plan_code: str,
                       datacenter: str = None, os_name: str = None,
                       duration: str = None, quantity: int = 1,
                       options: list = None) -> dict:
        """
        添加 Eco 服务器到购物车（完整流程）

        Args:
            cart_id: 购物车 ID
            plan_code: 服务器 planCode
            datacenter: 数据中心代码
            os_name: 操作系统
            duration: 时长
            quantity: 数量
            options: 硬件选项列表，如 ["ram-64g-ecc-3200-24rise", "softraid-2x960nvme-24rise"]
        """
        datacenter = datacenter or self.defaults.get("datacenter", "bhs")
        os_name = os_name or self.defaults.get("os", "none_64.en")
        duration = duration or self.defaults.get("duration", "P1M")

        # 1. 添加基础商品
        path = f"/order/cart/{cart_id}/eco"
        body = {
            "planCode": plan_code,
            "duration": duration,
            "pricingMode": "default",
            "quantity": quantity,
        }
        item_result = self.post(path, **body)
        item_id = item_result["itemId"]
        logger.info(f"已添加 Eco 服务器 {plan_code} 到购物车 {cart_id}, itemId={item_id}")

        # 2. 设置必需配置 (requiredConfiguration)
        region = get_region_for_dc(datacenter)
        configurations = {
            "dedicated_datacenter": datacenter,
            "dedicated_os": os_name,
        }
        if region:
            configurations["region"] = region

        for label, value in configurations.items():
            try:
                self.post(
                    f"/order/cart/{cart_id}/item/{item_id}/configuration",
                    label=label,
                    value=str(value),
                )
                logger.info(f"设置配置: {label} = {value}")
            except Exception as e:
                logger.warning(f"设置配置 {label} 失败: {e}")

        # 3. 添加硬件选项 (eco/options)
        if options and isinstance(options, list):
            hardware_options = self._filter_hardware_options(options)
            if hardware_options:
                try:
                    available_opts = self.get(
                        f"/order/cart/{cart_id}/eco/options",
                        planCode=plan_code,
                    )
                    logger.info(f"可用选项数: {len(available_opts)}")

                    added = 0
                    for wanted in hardware_options:
                        for avail in available_opts:
                            avail_pc = avail.get("planCode", "")
                            if avail_pc == wanted:
                                try:
                                    self.post(
                                        f"/order/cart/{cart_id}/eco/options",
                                        itemId=item_id,
                                        planCode=avail_pc,
                                        duration=avail.get("duration", duration),
                                        pricingMode=avail.get("pricingMode", "default"),
                                        quantity=1,
                                    )
                                    added += 1
                                    logger.info(f"添加硬件选项: {avail_pc}")
                                    break
                                except Exception as e:
                                    logger.warning(f"添加选项 {avail_pc} 失败: {e}")
                    logger.info(f"成功添加 {added}/{len(hardware_options)} 个硬件选项")
                except Exception as e:
                    logger.warning(f"获取 Eco 选项失败: {e}")

        return item_result

    def _filter_hardware_options(self, options: list) -> list:
        """过滤出硬件选项（排除软件/许可证）"""
        skip_terms = [
            "windows-server", "sql-server", "cpanel-license", "plesk-",
            "-license-", "os-", "control-panel", "panel", "license", "security",
        ]
        filtered = []
        for opt in options:
            if not opt or not isinstance(opt, str):
                continue
            opt_lower = opt.lower()
            if any(t in opt_lower for t in skip_terms):
                continue
            filtered.append(opt)
        return filtered

    def add_dedicated_server(self, cart_id: str, plan_code: str,
                             datacenter: str = None, os_name: str = None,
                             duration: str = None, quantity: int = 1) -> dict:
        """添加独立服务器到购物车"""
        datacenter = datacenter or self.defaults.get("datacenter", "bhs")
        os_name = os_name or self.defaults.get("os", "none_64.en")
        duration = duration or self.defaults.get("duration", "P1M")

        path = f"/order/cart/{cart_id}/dedicated/server"
        body = {
            "planCode": plan_code,
            "duration": duration,
            "pricingMode": "default",
            "quantity": quantity,
        }
        item_result = self.post(path, **body)
        item_id = item_result["itemId"]
        logger.info(f"已添加独立服务器 {plan_code} 到购物车 {cart_id}, itemId={item_id}")

        # 设置必需配置
        region = get_region_for_dc(datacenter)
        configurations = {
            "dedicated_datacenter": datacenter,
            "dedicated_os": os_name,
        }
        if region:
            configurations["region"] = region

        for label, value in configurations.items():
            try:
                self.post(
                    f"/order/cart/{cart_id}/item/{item_id}/configuration",
                    label=label,
                    value=str(value),
                )
            except Exception as e:
                logger.warning(f"设置配置 {label} 失败: {e}")

        return item_result

    def assign_cart(self, cart_id: str) -> dict:
        """分配购物车给当前用户"""
        return self.post(f"/order/cart/{cart_id}/assign")

    def checkout(self, cart_id: str, auto_pay: bool = False, waive_retract: bool = True) -> dict:
        """结账生成订单"""
        return self.post(
            f"/order/cart/{cart_id}/checkout",
            autoPayWithPreferredPaymentMethod=auto_pay,
            waiveRetractationPeriod=waive_retract,
        )

    def get_cart(self, cart_id: str) -> dict:
        return self.get(f"/order/cart/{cart_id}")

    def get_cart_summary(self, cart_id: str) -> dict:
        return self.get(f"/order/cart/{cart_id}/summary")

    def get_order(self, order_id: int) -> dict:
        return self.get(f"/me/order/{order_id}")

    def get_order_status(self, order_id: int) -> str:
        return self.get(f"/me/order/{order_id}/status")

    def get_order_details(self, order_id: int) -> dict:
        """获取订单详细信息 (含价格、状态)"""
        result = {"order_id": order_id, "status": None, "date": None,
                  "price_text": None, "price_value": None,
                  "payment_url": None, "order_url": None, "expiration_date": None}

        # 基本信息 (含价格)
        try:
            order = self.get(f"/me/order/{order_id}")
            result["date"] = order.get("date")
            result["expiration_date"] = order.get("expirationDate")
            result["order_url"] = order.get("url")
            pwt = order.get("priceWithTax", {})
            result["price_text"] = pwt.get("text")
            result["price_value"] = pwt.get("value")
        except Exception:
            pass

        # 状态 (单独端点)
        try:
            result["status"] = self.get(f"/me/order/{order_id}/status")
        except Exception:
            pass

        # 付款链接
        result["payment_url"] = self.get_payment_url(order_id)

        return result

    def list_recent_orders(self, offset: int = 0, count: int = 10) -> tuple:
        """获取订单列表 (分页) - 返回 (orders_list, total_count)"""
        try:
            orders = self.get("/me/order")
            if isinstance(orders, list):
                total = len(orders)
                orders_sorted = sorted(orders, reverse=True)
                page = orders_sorted[offset:offset + count]
                result = []
                for oid in page:
                    entry = {"order_id": oid, "status": "?", "date": None, "price_text": None}
                    try:
                        info = self.get(f"/me/order/{oid}")
                        entry["date"] = info.get("date")
                        pwt = info.get("priceWithTax", {})
                        entry["price_text"] = pwt.get("text")
                    except Exception:
                        pass
                    try:
                        entry["status"] = self.get(f"/me/order/{oid}/status")
                    except Exception:
                        pass
                    result.append(entry)
                return result, total
        except Exception:
            pass
        return [], 0

    # ---- 服务器管理 ----
    def list_servers(self) -> list:
        """列出所有独立服务器"""
        try:
            names = self.get("/dedicated/server")
            result = []
            for name in names:
                try:
                    info = self.get(f"/dedicated/server/{name}")
                    result.append({
                        "name": name,
                        "commercial_range": info.get("commercialRange", ""),
                        "os": info.get("os", ""),
                        "state": info.get("state", ""),
                        "power_state": info.get("powerState", ""),
                        "datacenter": info.get("datacenter", ""),
                        "ip": info.get("ip", ""),
                        "reverse": info.get("reverse", ""),
                        "monitoring": info.get("monitoring"),
                    })
                except Exception:
                    result.append({"name": name, "commercial_range": "?", "os": "?", "state": "?"})
            return result
        except Exception:
            return []

    def get_server_hardware(self, service_name: str) -> dict:
        """获取服务器硬件规格，含 diskGroups"""
        try:
            return self.get(f"/dedicated/server/{service_name}/specifications/hardware")
        except Exception:
            return {}

    def get_server_templates(self, service_name: str) -> list:
        """获取服务器可安装的 OS 模板列表"""
        try:
            r = self.get(f"/dedicated/server/{service_name}/install/compatibleTemplates")
            templates = []
            if isinstance(r, dict):
                for category, tlist in r.items():
                    for t in tlist:
                        templates.append(t)
            return sorted(templates)
        except Exception:
            return []

    def get_install_status(self, service_name: str) -> str:
        """获取当前安装状态"""
        try:
            return self.get(f"/dedicated/server/{service_name}/install/status")
        except Exception as e:
            return str(e)

    def list_ssh_keys(self) -> list:
        """列出 OVH 账号中预设的 SSH key"""
        try:
            return self.get("/me/sshKey")
        except Exception:
            return []

    def get_ssh_key_value(self, key_name: str) -> str:
        """读取 OVH 预设 SSH key 的公钥内容"""
        detail = self.get(f"/me/sshKey/{key_name}")
        return detail.get("key")

    def reinstall_server(self, service_name: str, template: str, hostname: str = None,
                         ssh_key_name: str = None, raid0: bool = False,
                         raid_disks: int = None, disk_group_id: int = None) -> dict:
        """重装系统 - 返回 task 信息"""
        body = {"operatingSystem": template}
        customizations = {}
        if hostname:
            customizations["hostname"] = hostname
        if ssh_key_name:
            customizations["sshKey"] = self.get_ssh_key_value(ssh_key_name)
        if customizations:
            body["customizations"] = customizations
        if raid0:
            partitioning = {
                "layout": [
                    {"mountPoint": "/", "fileSystem": "ext4", "raidLevel": 0, "size": 0}
                ],
            }
            if raid_disks is not None:
                partitioning["disks"] = raid_disks
            storage = {"diskGroupId": disk_group_id, "partitioning": partitioning}
            body["storage"] = [storage]
        return self.post(f"/dedicated/server/{service_name}/reinstall", **body)

    def reboot_server(self, service_name: str) -> dict:
        """硬重启服务器"""
        return self.post(f"/dedicated/server/{service_name}/reboot")

    def get_payment_url(self, order_id: int) -> str:
        """获取真实付款入口链接。finalPay 是未付款账单的直接付款页。"""
        zone_lower = self.zone.lower()
        return f"https://order.eu.ovhcloud.com/en-{zone_lower}/express/#/instant/finalPay?orderId={order_id}"

    def delete_cart(self, cart_id: str) -> dict:
        return self.delete(f"/order/cart/{cart_id}")

    # ---- 一键抢购（支持指定存储/内存） ----
    def quick_buy(self, plan_code: str, server_type: str = "eco",
                  datacenter: str = None, os_name: str = None,
                  options: list = None, target_dc: str = None,
                  target_storage: str = None,
                  target_memory: str = None) -> dict:
        """
        一键抢购 - 支持指定存储和内存配置

        Args:
            target_storage: 存储类型过滤: "nvme" / "hdd" / "2x500nvme" / "2x4hdd"
            target_memory: 内存大小过滤: "32g" / "64g"
        """
        result = {
            "success": False,
            "plan_code": plan_code,
            "server_type": server_type,
            "datacenter": datacenter or target_dc or self.defaults.get("datacenter", "bhs"),
            "config_info": None,
            "cart_id": None,
            "order_id": None,
            "payment_url": None,
            "price": None,
            "error": None,
            "elapsed": 0,
        }

        start_time = time.time()

        try:
            # 步骤 0: 检查可用性（按指定存储/内存过滤）
            dc = datacenter or target_dc
            available = self.find_available_configs(
                plan_code, target_dc=dc,
                target_storage=target_storage,
                target_memory=target_memory,
            )
            if not available:
                filter_desc = []
                if target_storage:
                    filter_desc.append(f"存储={target_storage}")
                if target_memory:
                    filter_desc.append(f"内存={target_memory}")
                if dc:
                    filter_desc.append(f"机房={dc}")
                filter_str = " ".join(filter_desc) if filter_desc else "全部配置"
                result["error"] = f"`{plan_code}` 指定配置({filter_str})当前无货"

                all_configs = self.check_availability(plan_code)
                if all_configs:
                    result["all_configs"] = all_configs
                result["elapsed"] = round(time.time() - start_time, 2)
                return result

            # 选择第一个符合条件且有货的配置
            chosen = available[0]
            actual_dc = chosen["datacenter"]
            result["datacenter"] = actual_dc
            result["config_info"] = {
                "memory_display": chosen["memory_display"],
                "storage_display": chosen["storage_display"],
                "memory": chosen["memory"],
                "storage": chosen["storage"],
            }
            logger.info(f"✅ 选择配置: {chosen['memory_display']} + {chosen['storage_display']} @ {actual_dc}")

            effective_options = options
            if not effective_options:
                effective_options = self._find_addon_options(
                    plan_code, chosen["memory"], chosen["storage"]
                )

            # 硬校验：指定配置必须能找到对应硬件选项，避免 OVH 默认落到其它内存/硬盘
            std_options = {self._standardize(o) for o in (effective_options or [])}
            expected_parts = []
            if chosen.get("memory") and chosen["memory"] != "N/A":
                expected_parts.append(("内存", chosen["memory"]))
            if chosen.get("storage") and chosen["storage"] != "N/A":
                expected_parts.append(("硬盘", chosen["storage"]))
            missing_parts = [label for label, value in expected_parts if self._standardize(value) not in std_options]
            if missing_parts:
                result["error"] = (
                    f"配置选项匹配失败，已阻止下单，避免买错配置。"
                    f"缺失: {', '.join(missing_parts)}；"
                    f"目标: {chosen['memory_display']} + {chosen['storage_display']}；"
                    f"已找到选项: {', '.join(effective_options or []) or '无'}"
                )
                result["elapsed"] = round(time.time() - start_time, 2)
                return result

            # 步骤 1: 创建购物车
            cart = self.create_cart()
            cart_id = cart["cartId"]
            result["cart_id"] = cart_id

            # 步骤 2: 添加服务器（带指定配置）
            if server_type == "eco":
                self.add_eco_server(
                    cart_id, plan_code,
                    datacenter=actual_dc,
                    os_name=os_name,
                    options=effective_options,
                )
            else:
                self.add_dedicated_server(
                    cart_id, plan_code,
                    datacenter=actual_dc,
                    os_name=os_name,
                )

            # 步骤 3: 分配购物车
            if self.defaults.get("auto_assign", True):
                self.assign_cart(cart_id)

            # 步骤 4: 获取价格
            try:
                summary = self.get_cart_summary(cart_id)
                prices = summary.get("prices", {})
                with_tax = prices.get("withTax", {})
                result["price"] = {
                    "withTax": with_tax.get("value") if isinstance(with_tax, dict) else with_tax,
                    "currencyCode": with_tax.get("currencyCode", "EUR") if isinstance(with_tax, dict) else "EUR",
                }
            except Exception as e:
                logger.warning(f"获取价格失败: {e}")

            # 步骤 5: 结账
            if self.defaults.get("auto_checkout", True):
                order = self.checkout(cart_id, auto_pay=False)
                result["order_id"] = order.get("orderId")
                result["payment_url"] = self.get_payment_url(order.get("orderId"))

            result["success"] = True

        except Exception as e:
            result["error"] = str(e)
            logger.error(f"抢购失败: {e}\n{traceback.format_exc()}")

        result["elapsed"] = round(time.time() - start_time, 2)
        return result

    def _find_addon_options(self, plan_code: str, memory: str, storage: str) -> list:
        """从 catalog 中查找匹配的 addon options"""
        options = []
        try:
            families = self.get_plan_addon_families(plan_code)
            for family in families:
                family_name = family.get("name", "").lower()
                addons = family.get("addons", [])
                if family_name == "memory" and memory and memory != "N/A":
                    mem_key = self._standardize(memory)
                    for addon in addons:
                        if self._standardize(addon) == mem_key:
                            options.append(addon)
                            break
                elif family_name == "storage" and storage and storage != "N/A":
                    stor_key = self._standardize(storage)
                    for addon in addons:
                        if self._standardize(addon) == stor_key:
                            options.append(addon)
                            break
        except Exception as e:
            logger.warning(f"查找 addon options 失败: {e}")
        return options

    @staticmethod
    def _standardize(config_str: str) -> str:
        """标准化配置字符串用于匹配"""
        if not config_str:
            return ""
        s = config_str.lower().strip()
        # 移除型号后缀
        patterns = [
            r'-\d{2}sk[a-z0-9]+(?:-v\d+)?$', r'-\d{2}rise[a-z0-9]+(?:-v\d+)?$',
            r'-\d+sk[a-z]+\d*', r'-\d+rise\d*', r'-\d+sys\w*',
            r'-\d+ska\d*', r'-\d+skstor\d*', r'-\d+skgame\d*',
            r'-\d+skc\d+', r'-\d+skb\d+', r'-ks\d+', r'-v\d+',
            r'-[a-z]{3}$',
        ]
        for p in patterns:
            s = re.sub(p, '', s)
        s = re.sub(r'-(no)?ecc-\d+', '', s)
        s = re.sub(r'-\d{4,5}$', '', s)
        return s


# ============================================================
# 消息解析辅助函数
# ============================================================
PLAN_CODE_PATTERNS = [
    # 匹配 OVH planCode: 24sk202, 26sk10b-v1, 24skstor012-v1, 24rise02-v1, 24adv01-v3, 25risel01-v1 等
    r'\b(\d{2}[a-z]+\w*(?:-v\d+)?)\b',
    r'\b(rise-\d+)\b',
    r'\b(advance-\d+)\b',
    r'\b(scale-\d+)\b',
    r'\b(game-\d+)\b',
    r'\b(stor-\d+)\b',
    r'\b(ks-[a-z\d]+(?:-[a-z\d]+)*)\b',
    r'\b(bv-\d+)\b',
    r'\b(host-\d+)\b',
    r'\b(grf-\d+)\b',
    r'\b(hgr-[a-z]+-\d+)\b',
]

# 服务器友好名称 → planCode 映射表
# 用户可以直接用名称，如 /watch ks-1-b fra nvme
SERVER_NAME_MAP = {
    # KS 系列
    "ks-1": "24sk102", "ks1": "24sk102",
    "ks-1-b": "26sk10b-v1", "ks1b": "26sk10b-v1", "ks-1b": "26sk10b-v1",
    "ks-2": "24sk202", "ks2": "24sk202",
    "ks-3": "24sk302", "ks3": "24sk302",
    "ks-4": "24sk402", "ks4": "24sk402",
    "ks-5": "24sk502", "ks5": "24sk502",
    "ks-5-a": "26sk50a-v1", "ks5a": "26sk50a-v1",
    "ks-5-b": "26sk50b-v1", "ks5b": "26sk50b-v1",
    "ks-6": "24sk602", "ks6": "24sk602",
    "ks-6-b": "25sk602b", "ks6b": "25sk602b",
    "ks-7": "24sk702", "ks7": "24sk702",
    "ks-a": "24ska012", "ksa": "24ska012",
    "ks-b": "25skb012", "ksb": "25skb012",
    "ks-c": "25skc012", "ksc": "25skc012",
    "ks-stor": "24skstor012-v1", "ksstor": "24skstor012-v1",
    "ks-game": "24skgame012", "ksgame": "24skgame012",
    # RISE 系列
    "rise-1": "24rise01-v1", "rise1": "24rise01-v1",
    "rise-2": "24rise02-v1", "rise2": "24rise02-v1",
    "rise-3": "24rise03-v1", "rise3": "24rise03-v1",
    "rise-4": "24rise04-v1", "rise4": "24rise04-v1",
    "rise-5": "24rise05-v1", "rise5": "24rise05-v1",
    "rise-6": "24rise06-v1", "rise6": "24rise06-v1",
    "rise-7": "24rise072", "rise7": "24rise072",
    "rise-8": "24rise082", "rise8": "24rise082",
    "rise-9": "24rise092", "rise9": "24rise092",
    "rise-l": "25risel01-v1", "risel": "25risel01-v1",
    "rise-s": "25rises01-v1", "rises": "25rises01-v1",
    "rise-m": "25risem01-v1", "risem": "25risem01-v1",
    "rise-xl": "25risexl01-v1", "risexl": "25risexl01-v1",
    "rise-stor": "24risestor012", "risestor": "24risestor012",
    "rise-game-1": "24risegame012", "risegame1": "24risegame012",
    "rise-game-2": "24risegame022", "risegame2": "24risegame022",
    # SYS 系列
    "sys-1": "24sys012", "sys1": "24sys012",
    "sys-2": "24sys022", "sys2": "24sys022",
    "sys-3": "24sys032", "sys3": "24sys032",
    "sys-4": "24sys043", "sys4": "24sys043",
    "sys-5": "24sys053", "sys5": "24sys053",
    "sys-6": "25sys062", "sys6": "25sys062",
    "sys-stor": "24sysstor012-v1", "sysstor": "24sysstor012-v1",
    "sys-game-1": "24sysgame012", "sysgame1": "24sysgame012",
    "sys-game-2": "24sysgame022", "sysgame2": "24sysgame022",
    # ADVANCE 系列
    "advance-1": "24adv01-v3", "advance1": "24adv01-v3",
    "advance-2": "24adv02-v3", "advance2": "24adv02-v3",
    "advance-3": "24adv03-v3", "advance3": "24adv03-v3",
    "advance-4": "24adv04-v3", "advance4": "24adv04-v3",
    "advance-5": "24adv05-v3", "advance5": "24adv05-v3",
    "advance-stor": "24advstor01-v3", "advancestor": "24advstor01-v3",
}


def resolve_plan_code(text: str) -> str:
    """解析服务器型号 - 支持友好名称和 planCode

    输入: ks-1-b / ks1b / KS-1-B / 26sk10b-v1 → 输出: 26sk10b-v1
    """
    if not text:
        return None

    text_lower = text.lower().strip()

    # 0. 如果本身就是完整 planCode（直接包含数字+字母格式），直接返回
    if re.match(r'^\d{2}\w+$', text_lower):
        return text_lower

    # 1. 查友好名称映射表
    if text_lower in SERVER_NAME_MAP:
        return SERVER_NAME_MAP[text_lower]

    # 2. 正则匹配 planCode 格式
    for pattern in PLAN_CODE_PATTERNS:
        m = re.search(pattern, text_lower)
        if m:
            return m.group(1)

    return None

DATACENTER_MAP = {
    "bhs": "bhs", "beauharnois": "bhs", "加拿大": "bhs",
    "gra": "gra", "gravelines": "gra",
    "sbg": "sbg", "strasbourg": "sbg", "斯特拉斯堡": "sbg",
    "rbx": "rbx", "roubaix": "rbx",
    "par": "par", "paris": "par", "巴黎": "par",
    "fra": "fra", "frankfurt": "fra", "法兰克福": "fra",
    "lon": "lon", "london": "lon", "伦敦": "lon",
    "waw": "waw", "warsaw": "waw", "华沙": "waw",
    "eri": "eri", "erlangen": "eri",
    "vin": "vin", "vint-hill": "vin",
    "sgp": "sgp", "singapore": "sgp", "新加坡": "sgp",
    "syd": "syd", "sydney": "syd",
    "ynm": "ynm", "mumbai": "ynm", "孟买": "ynm",
}

DC_DISPLAY_MAP = {
    "bhs": "🇨🇦 博阿努瓦",
    "gra": "🇫🇷 格拉沃利讷",
    "sbg": "🇫🇷 斯特拉斯堡",
    "rbx": "🇫🇷 鲁贝",
    "par": "🇫🇷 巴黎",
    "fra": "🇩🇪 法兰克福",
    "lon": "🇬🇧 伦敦",
    "waw": "🇵🇱 华沙",
    "eri": "🇩🇪 埃尔朗根",
    "vin": "🇺🇸 文特希尔",
    "hil": "🇩🇪 希勒斯多夫",
    "sgp": "🇸🇬 新加坡",
    "syd": "🇦🇺 悉尼",
    "ynm": "🇮🇳 孟买",
}

STATUS_CN_MAP = {
    "unavailable": "无货",
    "unknown": "未知",
    "available": "有货",
    "1H": "少量",
    "72H": "72小时",
    "restock": "补货中",
    "comingSoon": "即将到货",
}


def format_dc_status(status: str) -> str:
    """将 OVH 状态翻译成中文"""
    if not status:
        return "未知"
    s = status.lower()
    if s in UNAVAILABLE_STATES:
        return "无货"
    return STATUS_CN_MAP.get(s, "有货" if s != "unavailable" else "无货")


def format_dc(dc: str) -> str:
    """返回中文机房名"""
    return DC_DISPLAY_MAP.get(dc, dc)


def parse_plan_code(text: str):
    """从文本中提取 planCode（兼容旧调用，内部使用 resolve_plan_code）"""
    text_lower = text.lower()
    # 1. 先查友好名称
    for name, pc in SERVER_NAME_MAP.items():
        if name in text_lower:
            return pc
    # 2. 正则匹配
    for pattern in PLAN_CODE_PATTERNS:
        m = re.search(pattern, text_lower)
        if m:
            return m.group(1)
    return None


def parse_datacenter(text: str):
    text = text.lower()
    for keyword, dc in DATACENTER_MAP.items():
        if keyword in text:
            return dc
    return None


def guess_server_type(plan_code: str) -> str:
    plan_code = plan_code.lower()
    if plan_code.startswith(("ks-", "bv-")):
        return "dedicated"
    return "eco"


# ============================================================
# Telegram Bot
# ============================================================
def run_bot(cfg: dict):
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import (
        ApplicationBuilder,
        CommandHandler,
        MessageHandler,
        CallbackQueryHandler,
        ContextTypes,
        filters,
    )

    tg_cfg = cfg.get("telegram", {})
    bot_token = tg_cfg.get("bot_token", "")
    allowed_users = tg_cfg.get("allowed_users", [])
    bot_app = None

    if not bot_token:
        logger.error("未配置 Telegram Bot Token")
        sys.exit(1)

    ovh_client = OVHClient(cfg)

    def check_user(user_id: int) -> bool:
        if not allowed_users:
            return True
        return user_id in allowed_users

    async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not check_user(update.effective_user.id):
            await update.message.reply_text("⛔ 未授权")
            return
        await update.message.reply_text(
            "🤖 *OVH 抢购 Bot 已就绪*\n\n"
            "常用入口:\n"
            "🛒 /buy `型号` - 只显示当前有货配置，按钮抢购\n"
            "📡 /watch `型号` - 显示全部配置，按钮设置监控\n"
            "📋 /watchlist - 查看、暂停、启用、删除监控\n"
            "💳 /status - 查看最近订单\n"
            "🖥️ /servers - 服务器列表、重装、重启\n\n"
            "输入 /help 查看完整说明。\n"
            f"🌐 当前区域: `{ovh_client.zone}` / `{ovh_client.subsidiary}`",
            parse_mode="Markdown",
        )

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not check_user(update.effective_user.id):
            return
        await update.message.reply_text(
            "📖 *OVH Bot 帮助*\n\n"
            "🛒 *抢购*\n"
            "/buy `型号`\n"
            "只列出当前有货的配置和机房，按按钮选择配置、机房、数量后下单。\n\n"
            "/check `型号`\n"
            "查看该型号全部配置、全部机房的库存状态。\n\n"
            "📡 *监控*\n"
            "/watch `型号`\n"
            "列出全部配置，包括当前无货配置；按按钮选择配置、机房、下单上限。\n\n"
            "/watchlist\n"
            "查看监控进度，并可按钮暂停、启用、删除任务。已达上限的任务重新启用会自动重置进度。\n\n"
            "/unwatch `型号`\n"
            "删除指定监控；不带型号时删除全部监控。\n\n"
            "💳 *订单*\n"
            "/status\n"
            "查看最近订单，支持翻页。\n\n"
            "/status `订单号`\n"
            "查看订单详情、状态、价格和待付款链接。\n\n"
            "/pay `订单号`\n"
            "获取指定订单付款链接。\n\n"
            "🖥️ *服务器*\n"
            "/servers\n"
            "查看服务器列表；按钮执行重装、重启。重装流程会自动识别磁盘组和 RAID0 选项。\n\n"
            "/keys\n"
            "查看 OVH 账户里的预设 SSH 密钥。\n\n"
            "📦 *目录*\n"
            "/catalog\n"
            "查看服务器目录。\n\n"
            "💡 多数流程支持按钮返回上一步，取消会直接删除当前菜单消息。\n"
            f"🌐 当前区域: `{ovh_client.zone}` / `{ovh_client.subsidiary}`",
            parse_mode="Markdown",
        )

    async def buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not check_user(update.effective_user.id):
            await update.message.reply_text("⛔ 未授权")
            return

        if not context.args:
            await update.message.reply_text(
                "用法: /buy <planCode>\n\n"
                "示例: /buy ks-1-b\n\n"
                "然后用按钮选择配置和机房。"
            )
            return

        plan_code = resolve_plan_code(context.args[0])
        if not plan_code:
            await update.message.reply_text(f"❌ 无法识别型号: {context.args[0]}\n\n可用名称: ks-1-b, ks-stor, ks-2, rise-2 等")
            return

        msg = await update.message.reply_text(f"🔍 正在查询 `{plan_code}` 可抢配置...", parse_mode="Markdown")
        all_configs = ovh_client.check_availability(plan_code)
        if not all_configs:
            await msg.edit_text(f"❌ 未获取到 `{plan_code}` 的可用性数据", parse_mode="Markdown")
            return

        available_cfgs = []
        for cfg in all_configs:
            if any(status not in UNAVAILABLE_STATES for status in cfg["datacenters"].values()):
                available_cfgs.append(cfg)

        if not available_cfgs:
            await msg.edit_text(
                f"❌ `{plan_code}` 当前没有任何有货配置，无法抢购。\n\n"
                f"💡 请用 /watch 先设定监控，等有货后自动下单。",
                parse_mode="Markdown"
            )
            return

        session_id = str(int(time.time() * 1000))[-10:]
        buy_sessions[session_id] = {
            "plan_code": plan_code,
            "all_configs": all_configs,
            "display_configs": available_cfgs,
            "selected_cfg": None,
            "selected_dc": None,
            "target_storage": None,
            "target_memory": None,
            "count": 1,
        }

        buttons = []
        for idx, cfg in enumerate(available_cfgs[:20]):
            buttons.append([InlineKeyboardButton(
                f"#{idx+1} {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}",
                callback_data=f"buy|cfg|{session_id}|{idx}"
            )])

        text = f"🛒 *选择要抢购的配置*\n\n型号: `{plan_code}`\n\n只显示当前有货配置。"
        buttons.append([InlineKeyboardButton("取消", callback_data="cancel")])
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    async def check_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not check_user(update.effective_user.id):
            return

        if not context.args:
            await update.message.reply_text("用法: /check <planCode>\n示例: /check ks-1-b")
            return

        plan_code = resolve_plan_code(context.args[0])
        if not plan_code:
            await update.message.reply_text(f"❌ 无法识别型号: {context.args[0]}\n\n可用名称: ks-1-b, ks-stor, ks-2, rise-2 等")
            return
        msg = await update.message.reply_text(f"🔍 正在查询 `{plan_code}` 所有配置的可用性...", parse_mode="Markdown")

        all_configs = ovh_client.check_availability(plan_code)
        if not all_configs:
            await msg.edit_text(f"❌ 未获取到 `{plan_code}` 的可用性数据", parse_mode="Markdown")
            return

        # 获取基础价格（从 catalog）
        base_price_str = ""
        try:
            catalog = ovh_client.get_catalog('eco')
            for plan in catalog.get('plans', []):
                if plan.get('planCode') == plan_code:
                    pricings = plan.get('pricings', [])
                    for p in pricings:
                        if p.get('capacities') == ['installation'] and p.get('phase') == 0:
                            install = p.get('formattedPrice', '')
                        if p.get('capacities') == ['renew'] and p.get('interval') == 1:
                            monthly = p.get('formattedPrice', '')
                    invoice_name = plan.get('invoiceName', '')
                    base_price_str = f"\n💰 基础价: {monthly}/月 + {install} 安装费"
                    break
        except Exception:
            pass

        # 收集有货的配置（需要查价格）
        available_configs_to_price = []
        for cfg in all_configs:
            for dc, status in cfg["datacenters"].items():
                if status not in UNAVAILABLE_STATES:
                    available_configs_to_price.append((cfg, dc, status))

        # 有货的才实时查价（避免无货时浪费时间）
        price_cache = {}  # key=fqn|dc, value=price_str
        if available_configs_to_price:
            await msg.edit_text(f"🔍 查询可用性中...（{len(available_configs_to_price)} 个有货配置查价格中）", parse_mode="Markdown")
            for cfg, dc, status in available_configs_to_price:
                try:
                    price = ovh_client.get_config_price(plan_code, dc, cfg["memory"], cfg["storage"])
                    if price:
                        price_cache[f"{cfg['fqn']}|{dc}"] = price
                except Exception as e:
                    logger.warning(f"查价失败 {cfg['fqn']}@{dc}: {e}")

        text = f"📊 *{plan_code} 可用性报告*{base_price_str}\n（共 {len(all_configs)} 个配置组合）\n\n"
        buttons = []

        for idx, cfg in enumerate(all_configs):
            mem_display = format_memory(cfg["memory"])
            stor_display = format_storage(cfg["storage"])
            stor_raw = cfg["storage"].lower()

            stor_keyword = ""
            if "nvme" in stor_raw:
                m = re.search(r'(\d+x\d+nvme)', stor_raw)
                stor_keyword = m.group(1) if m else "nvme"
            elif "sa" in stor_raw:
                m = re.search(r'(\d+x\d+)sa', stor_raw)
                stor_keyword = (m.group(1) + "hdd") if m else "hdd"

            text += f"📦 *#{idx+1} {mem_display} + {stor_display}*\n"

            has_available = False
            for dc, status in cfg["datacenters"].items():
                dc_display = format_dc(dc)
                status_cn = format_dc_status(status)
                key = f"{cfg['fqn']}|{dc}"
                price_str = price_cache.get(key, "")
                if status in UNAVAILABLE_STATES:
                    text += f"   ❌ {dc_display}: {status_cn}\n"
                else:
                    has_available = True
                    price_text = f" 💰{price_str}" if price_str else ""
                    text += f"   ✅ {dc_display}: {status_cn}{price_text}\n"
                    btn_label = f"🛒#{idx+1} {stor_display} @{dc}"
                    callback = f"buy|preset|{plan_code}|{dc}|{stor_keyword}"
                    buttons.append([InlineKeyboardButton(btn_label, callback_data=callback)])

            text += "\n"

        if not any(s not in UNAVAILABLE_STATES for cfg in all_configs for s in cfg["datacenters"].values()):
            text += "😢 当前所有配置均无货"

        buttons.append([InlineKeyboardButton("取消", callback_data="cancel")])
        reply_markup = InlineKeyboardMarkup(buttons)
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)

    # ---- 内置监控器 ----
    # 监控任务: {plan_code: {"dc": str|None, "storage": str|None, "memory": str|None,
    #                         "max_orders": int, "ordered": int, "active": bool}}
    import os as _os
    DATA_DIR = _os.environ.get("OVH_BOT_DATA_DIR", "/app/data")
    WATCH_FILE = _os.path.join(DATA_DIR, "watch_tasks.json")

    def save_watch_tasks():
        """持久化监控任务到文件"""
        try:
            _os.makedirs(DATA_DIR, exist_ok=True)
            serializable = {}
            for pc, task in watch_tasks.items():
                serializable[pc] = {k: v for k, v in task.items() if not k.startswith("_")}
            with open(WATCH_FILE, "w") as f:
                json.dump(serializable, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存监控任务失败: {e}")

    def load_watch_tasks():
        """从文件加载监控任务"""
        try:
            if _os.path.exists(WATCH_FILE):
                with open(WATCH_FILE, "r") as f:
                    data = json.load(f)
                for pc, task in data.items():
                    task["_last_order_time"] = {}
                    watch_tasks[pc] = task
                if watch_tasks:
                    logger.info(f"从文件恢复 {len(watch_tasks)} 个监控任务")
        except Exception as e:
            logger.warning(f"加载监控任务失败: {e}")

    watch_tasks = {}
    load_watch_tasks()  # 启动时恢复
    pending_actions = {}
    watch_sessions = {}
    buy_sessions = {}
    watch_lock = asyncio.Lock() if hasattr(asyncio, 'Lock') else None

    async def watch_monitor_loop():
        """后台监控循环"""
        nonlocal watch_running
        while watch_running:
            try:
                for plan_code, task in list(watch_tasks.items()):
                    if not task["active"]:
                        continue
                    if task["ordered"] >= task["max_orders"]:
                        task["active"] = False
                        save_watch_tasks()
                        await _send_msg(f"🎯 `{plan_code}` 已达到下单上限 ({task['max_orders']}单)，监控自动停止", task.get("chat_id"))
                        continue

                    try:
                        available = ovh_client.find_available_configs(
                            plan_code,
                            target_dc=task.get("dc"),
                            target_storage=task.get("storage"),
                            target_memory=task.get("memory"),
                        )
                        if available:
                            chosen = available[0]
                            # 检查 2 分钟内是否刚下过同款
                            cooldown_key = f"{plan_code}|{chosen['datacenter']}|{chosen['fqn']}"
                            now = time.time()
                            last_order_time = task.get("_last_order_time", {})
                            if cooldown_key in last_order_time:
                                if now - last_order_time[cooldown_key] < 120:
                                    continue

                            stor_str = f" {task['storage']}" if task.get("storage") else ""
                            dc_display = format_dc(chosen['datacenter'])
                            await _send_msg(
                                f"🔥 *监控发现 `{plan_code}` 有货！*\n"
                                f"📍 {dc_display} | {chosen['memory_display']} + {chosen['storage_display']}\n"
                                f"🚀 正在自动下单... ({task['ordered']+1}/{task['max_orders']})",
                                task.get("chat_id")
                            )

                            server_type = guess_server_type(plan_code)
                            result = ovh_client.quick_buy(
                                plan_code=plan_code,
                                server_type=server_type,
                                datacenter=chosen["datacenter"],
                                target_storage=chosen.get("storage") or task.get("storage"),
                                target_memory=chosen.get("memory") or task.get("memory"),
                            )

                            if result["success"]:
                                task["ordered"] += 1
                                last_order_time[cooldown_key] = now
                                task["_last_order_time"] = last_order_time
                                save_watch_tasks()

                                # 精简的成功消息
                                text = f"✅ *监控自动下单成功！*\n\n"
                                text += f"📦 服务器: `{result['plan_code']}`\n"
                                text += f"🏗️ 机房: {format_dc(result['datacenter'])}\n"
                                ci = result.get("config_info")
                                if ci:
                                    text += f"💾 配置: {ci['memory_display']} + {ci['storage_display']}\n"
                                if result.get("price"):
                                    p = result["price"]
                                    text += f"💰 价格: {p.get('withTax', '?')} {p.get('currencyCode', 'EUR')}\n"
                                if result["order_id"]:
                                    text += f"📋 订单号: `{result['order_id']}`\n"
                                if result["payment_url"]:
                                    text += f"💳 付款链接: {result['payment_url']}\n"
                                text += f"\n📊 监控进度: 已下 {task['ordered']}/{task['max_orders']} 单"
                                if task["ordered"] >= task["max_orders"]:
                                    task["active"] = False
                                    save_watch_tasks()
                                    text += "\n🎯 已达上限，监控自动停止\n\n⚠️ 请尽快手动付款以锁定订单！"
                                else:
                                    text += "\n\n⚠️ 请尽快手动付款以锁定订单！"
                            else:
                                # 失败也加短冷却，避免库存瞬时变化时疯狂刷屏/重复请求
                                last_order_time[cooldown_key] = now
                                task["_last_order_time"] = last_order_time
                                save_watch_tasks()
                                text = f"❌ 监控自动下单失败: `{plan_code}`\n{result['error']}"

                            await _send_msg(text, task.get("chat_id"))
                    except Exception as e:
                        logger.error(f"监控 {plan_code} 出错: {e}")

            except Exception as e:
                logger.error(f"监控循环出错: {e}")

            await asyncio.sleep(10)  # 每 10 秒检查一次

    async def _send_msg(text: str, chat_id: str = None):
        """发送消息到指定 chat；未指定则回退到默认 chat"""
        try:
            target_chat_id = str(chat_id or tg_cfg.get("chat_id", ""))
            if not target_chat_id or bot_app is None:
                logger.error(f"发送监控消息失败: chat_id={target_chat_id}, bot_app={bot_app is not None}")
                return
            try:
                await bot_app.bot.send_message(chat_id=target_chat_id, text=text, parse_mode="Markdown")
            except Exception as markdown_error:
                logger.error(f"监控 Markdown 消息发送失败，改用纯文本: {markdown_error}")
                await bot_app.bot.send_message(chat_id=target_chat_id, text=text)
        except Exception as e:
            logger.error(f"发送监控消息失败: {e}")

    def _progress_bar(percent: int, width: int = 12) -> str:
        percent = max(0, min(100, int(percent)))
        filled = round(width * percent / 100)
        return "█" * filled + "░" * (width - filled)

    def _extract_install_progress(status_obj, elapsed_sec: int = 0):
        """从 OVH 安装状态中提取阶段和百分比；缺少百分比时按耗时给保守估算。"""
        if isinstance(status_obj, dict):
            status_text = str(status_obj.get("status") or status_obj.get("state") or status_obj.get("step") or status_obj)
            for key in ("progress", "percentage", "percent"):
                val = status_obj.get(key)
                if isinstance(val, (int, float)):
                    return status_text, int(val), False
        else:
            status_text = str(status_obj)

        lower = status_text.lower()
        if "not being installed" in lower or "not being reinstalled" in lower:
            return "安装已结束或 OVH 暂无安装状态", 100, True
        if "error" in lower or "fail" in lower:
            return status_text, 100, True
        # OVH 有些账号只返回文本状态，没有百分比；用耗时做保守估算，最多 95%，完成由状态接口判断。
        estimated = min(95, max(5, int(elapsed_sec / 18)))  # 约 30 分钟到 95%
        return status_text, estimated, False

    async def track_install_progress(message, service_name: str, template: str, task_id: str = "?",
                                     ssh_key_name: str = None, raid_text: str = None):
        """后台轮询安装状态并编辑同一条消息显示进度条。"""
        start_ts = time.time()
        last_text = None
        for _ in range(120):  # 最多跟踪约 40 分钟
            try:
                elapsed = int(time.time() - start_ts)
                status_obj = ovh_client.get_install_status(service_name)
                status_text, percent, done = _extract_install_progress(status_obj, elapsed)
                bar = _progress_bar(percent)
                mins, secs = divmod(elapsed, 60)
                text = (
                    f"💿 *系统安装进度*\n\n"
                    f"🖥️ 服务器: `{service_name}`\n"
                    f"💿 系统: `{template}`\n"
                    + (f"🔑 SSH密钥: `{ssh_key_name}`\n" if ssh_key_name else "")
                    + (f"🧩 磁盘: `{raid_text}`\n" if raid_text else "")
                    + f"📋 任务ID: `{task_id}`\n\n"
                    f"`{bar}` {percent}%\n"
                    f"📌 状态: `{status_text}`\n"
                    f"⏱️ 耗时: {mins}分{secs}秒"
                )
                if done:
                    text += "\n\n✅ 安装状态已结束，请用 /servers 确认当前系统。"
                else:
                    text += "\n\n⏳ Bot 会自动刷新此进度。"

                if text != last_text:
                    await message.edit_text(text, parse_mode="Markdown")
                    last_text = text
                if done:
                    return
                await asyncio.sleep(20)
            except Exception as e:
                logger.error(f"刷新安装进度失败: {e}")
                await asyncio.sleep(20)

    async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """开始监控服务器 - 通过按钮选择配置和机房"""
        if not check_user(update.effective_user.id):
            await update.message.reply_text("⛔ 未授权")
            return

        if not context.args:
            await update.message.reply_text(
                "用法: /watch <planCode>\n\n"
                "示例: /watch ks-1-b\n\n"
                "然后用按钮选择配置和机房"
            )
            return

        plan_code = resolve_plan_code(context.args[0])
        if not plan_code:
            await update.message.reply_text(f"❌ 无法识别型号: {context.args[0]}\n\n可用名称: ks-1-b, ks-stor, ks-2, rise-2 等")
            return

        msg = await update.message.reply_text(f"🔍 正在查询 `{plan_code}` 可监控配置...", parse_mode="Markdown")
        all_configs = ovh_client.check_availability(plan_code)
        if not all_configs:
            await msg.edit_text(f"❌ 未获取到 `{plan_code}` 的可用性数据", parse_mode="Markdown")
            return

        available_cfgs = []
        for cfg in all_configs:
            for dc, status in cfg["datacenters"].items():
                if status not in UNAVAILABLE_STATES:
                    available_cfgs.append(cfg)
                    break

        source_cfgs = all_configs
        session_id = str(int(time.time() * 1000))[-10:]
        watch_sessions[session_id] = {
            "plan_code": plan_code,
            "all_configs": all_configs,
            "display_configs": source_cfgs,
            "selected_fqn": None,
            "selected_dc": None,
            "max_orders": 1,
        }

        buttons = []
        for idx, cfg in enumerate(source_cfgs[:20]):
            buttons.append([InlineKeyboardButton(
                f"#{idx+1} {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}",
                callback_data=f"watch|cfg|{session_id}|{idx}"
            )])

        text = f"📡 *选择要监控的配置*\n\n型号: `{plan_code}`\n"
        text += "\n监控会列出全部配置，无货配置也可以先设定，等有货后自动下单。"
        buttons.append([InlineKeyboardButton("取消", callback_data="cancel")])
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    async def unwatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """取消监控"""
        if not check_user(update.effective_user.id):
            return

        if not context.args:
            # 取消所有监控
            count = sum(1 for t in watch_tasks.values() if t["active"])
            if count == 0:
                await update.message.reply_text("📭 当前没有监控任务")
                return
            for pc in watch_tasks:
                watch_tasks[pc]["active"] = False
            watch_tasks.clear()
            save_watch_tasks()
            await update.message.reply_text(f"📭 已取消所有监控 ({count} 个)")
            return

        plan_code = resolve_plan_code(context.args[0])
        if not plan_code:
            await update.message.reply_text(f"❌ 无法识别型号: {context.args[0]}\n\n可用名称: ks-1-b, ks-stor, ks-2, rise-2 等")
            return
        if plan_code in watch_tasks:
            watch_tasks[plan_code]["active"] = False
            del watch_tasks[plan_code]
            save_watch_tasks()
            await update.message.reply_text(f"📭 已取消监控 `{plan_code}`", parse_mode="Markdown")
        else:
            await update.message.reply_text(f"⚠️ `{plan_code}` 不在监控列表中", parse_mode="Markdown")

    async def watchlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """查看当前监控列表"""
        if not check_user(update.effective_user.id):
            return

        if not watch_tasks:
            await update.message.reply_text("📭 当前没有监控任务\n\n用 /watch <planCode> 开始监控")
            return

        text = "📡 *当前监控列表*\n\n"
        for pc, task in watch_tasks.items():
            status = "🟢 监控中" if task["active"] else "🔴 已停止"
            filter_parts = []
            if task.get("dc"):
                dc_display = format_dc(task['dc']) if task['dc'] else "全部机房"
                filter_parts.append(f"机房={dc_display}")
            if task.get("storage"):
                filter_parts.append(f"存储={format_storage(task['storage'])}")
            if task.get("memory"):
                filter_parts.append(f"内存={format_memory(task['memory'])}")
            filter_str = f" ({', '.join(filter_parts)})" if filter_parts else ""

            text += (
                f"{status} `{pc}`{filter_str}\n"
                f"   进度: {task['ordered']}/{task['max_orders']} 单\n\n"
            )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ 管理监控", callback_data="watchlist|manage")],
            [InlineKeyboardButton("取消", callback_data="cancel")],
        ])
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

    async def catalog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not check_user(update.effective_user.id):
            return

        category = context.args[0] if context.args else "eco"
        msg = await update.message.reply_text(f"📖 正在获取 {category} 服务器目录...")

        catalog = ovh_client.get_catalog(category)
        if not catalog:
            await msg.edit_text("❌ 获取目录失败")
            return

        plans = catalog.get("plans", [])
        if not plans:
            await msg.edit_text("❌ 目录为空")
            return

        text = f"📖 *{category.upper()} 服务器目录* ({len(plans)} 个)\n\n"
        for plan in plans[:30]:
            pc = plan.get("planCode", "?")
            invoice_name = plan.get("invoiceName", "")
            if invoice_name:
                text += f"• `{pc}` - {invoice_name}\n"
            else:
                text += f"• `{pc}`\n"

        if len(plans) > 30:
            text += f"\n... 还有 {len(plans) - 30} 个型号"

        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("取消", callback_data="cancel")]])
        await msg.edit_text(text, parse_mode="Markdown", reply_markup=keyboard)

    async def pay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not check_user(update.effective_user.id):
            return
        if not context.args:
            await update.message.reply_text("用法: /pay <orderId>")
            return

        try:
            order_id = int(context.args[0])
            url = ovh_client.get_payment_url(order_id)
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("取消", callback_data="cancel")]])
            await update.message.reply_text(
                f"💳 订单 `{order_id}` 付款链接:\n\n{url}\n\n⚠️ 请尽快付款！",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
        except Exception as e:
            await update.message.reply_text(f"❌ 获取付款链接失败: {e}")

    async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not check_user(update.effective_user.id):
            return

        STATUS_MAP = {
            "delivered": ("✅", "Complete"),
            "delivering": ("🔄", "Being processed"),
            "pendingPayment": ("⏳", "Pending payment"),
            "notPaid": ("⏳", "Not paid"),
            "validatingPayment": ("🔄", "Validating payment"),
            "pending_debit_validation": ("⏳", "Pending validation"),
            "canceled": ("❌", "Canceled"),
            "expired": ("💀", "Expired"),
            "unknown": ("❓", "Unknown"),
        }

        def fmt_status(s):
            emoji, label = STATUS_MAP.get(s, ("📌", s))
            return f"{emoji} {label}"

        if not context.args:
            try:
                msg = await update.message.reply_text("⏳ 正在查询订单...")
                orders, total = ovh_client.list_recent_orders(0, 10)
                if not orders:
                    await update.message.reply_text("📭 没有找到订单")
                    return

                lines = ["📋 *最近订单*（同 OVH 官网）\n"]
                for o in orders:
                    date_str = to_bjt(o["date"])[:10] if o.get("date") else "N/A"
                    price_str = o.get("price_text") or ""
                    status_str = fmt_status(o["status"])
                    lines.append(f"{date_str}  `{o['order_id']}`\n   {status_str}  {price_str}\n")

                lines.append(f"\n💡 `/status <订单号>` 查看详情")
                lines.append(f"📄 共 {total} 个订单")

                keyboard = []
                if total > 10:
                    keyboard.append([InlineKeyboardButton("▶️ 下一页", callback_data="orders|p|1")])
                keyboard.append([InlineKeyboardButton("取消", callback_data="cancel")])

                await msg.edit_text(
                    "\n".join(lines),
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except Exception as e:
                await update.message.reply_text(f"❌ 查询失败: {e}")
            return

        try:
            order_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ 订单号必须是数字\n用法: /status 254452143")
            return

        try:
            msg = await update.message.reply_text(f"⏳ 正在查询订单 `{order_id}`...", parse_mode="Markdown")

            detail = ovh_client.get_order_details(order_id)
            status = detail.get("status", "unknown")

            lines = [f"📋 *订单* `{order_id}`\n"]
            lines.append(f"状态: {fmt_status(status)}")
            if detail.get("date"):
                lines.append(f"日期: {to_bjt(detail['date'])}")
            if detail.get("price_text"):
                lines.append(f"💰 价格: {detail['price_text']}")
            if detail.get("expiration_date"):
                lines.append(f"到期: {to_bjt(detail['expiration_date'])}")

            pay_url = detail.get("payment_url")
            unpaid = status in ("pendingPayment", "pending_debit_validation", "notPaid")
            if pay_url and unpaid:
                lines.append(f"\n💳 [点击付款]({pay_url})")

            order_url = detail.get("order_url")
            if order_url:
                lines.append(f"📄 [OVH 订单页面]({order_url})")

            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("取消", callback_data="cancel")]])
            await msg.edit_text("\n".join(lines), parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            await update.message.reply_text(f"❌ 查询失败: {e}")

    async def servers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """列出所有独立服务器"""
        if not check_user(update.effective_user.id):
            return

        msg = await update.message.reply_text("⏳ 正在获取服务器列表...")
        try:
            servers = ovh_client.list_servers()
            if not servers:
                await msg.edit_text("📭 没有找到独立服务器")
                return

            lines = [f"🖥️ 独立服务器列表 ({len(servers)} 台)\n"]
            keyboard = []
            for i, s in enumerate(servers):
                state_emoji = {"ok": "🟢", "error": "🔴"}.get(s.get("state", ""), "🟡")
                hw = ovh_client.get_server_hardware(s["name"])
                disk_groups = hw.get("diskGroups", []) if isinstance(hw, dict) else []
                default_group = hw.get("defaultDiskGroupId") if isinstance(hw, dict) else None

                lines.append(f"{state_emoji} {i+1}. {s['name']}")
                lines.append(f"   📦 {s.get('commercial_range','?')}")
                lines.append(f"   💻 {s.get('os','?')} | 📍 {s.get('datacenter','?')}")
                if s.get("ip"):
                    lines.append(f"   🌐 {s['ip']}")
                if disk_groups:
                    lines.append("   💽 磁盘组:")
                    for dg in disk_groups:
                        size = dg.get("diskSize", {})
                        size_txt = f"{size.get('value','?')}{size.get('unit','')}"
                        mark = " (默认)" if dg.get("diskGroupId") == default_group else ""
                        lines.append(
                            f"      group={dg.get('diskGroupId')} {dg.get('numberOfDisks')}x {dg.get('diskType')} {size_txt}{mark}"
                        )
                lines.append("")

                action_id = f"srv{i+1}_{str(int(time.time() * 1000))[-6:]}"
                pending_actions[action_id] = {
                    "type": "server", "service_name": s["name"], "index": i+1,
                    "disk_groups": disk_groups, "default_group": default_group
                }
                keyboard.append([
                    InlineKeyboardButton(f"💿 安装 {i+1}", callback_data=f"srv|install|{action_id}"),
                    InlineKeyboardButton(f"🔄 重启 {i+1}", callback_data=f"srv|reboot|{action_id}"),
                ])

            lines.append("💡 点按钮即可安装系统或重启服务器")
            keyboard.append([InlineKeyboardButton("取消", callback_data="cancel")])
            await msg.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            await msg.edit_text(f"❌ 获取失败: {e}")

    async def keys_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """列出 OVH 预设 SSH 密钥"""
        if not check_user(update.effective_user.id):
            return
        try:
            keys = ovh_client.list_ssh_keys()
            if not keys:
                await update.message.reply_text("📭 OVH 账号里没有预设 SSH 密钥")
                return
            text = "🔑 *OVH 预设 SSH 密钥*\n\n" + "\n".join(f"• `{k}`" for k in keys)
            text += "\n\n💡 安装系统请用 /servers 按钮流程选择密钥"
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("取消", callback_data="cancel")]])
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            await update.message.reply_text(f"❌ 获取密钥失败: {e}")

    async def reinstall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """安装/重装系统"""
        if not check_user(update.effective_user.id):
            return

        if not context.args:
            await update.message.reply_text(
                "安装系统请使用 /servers 按钮流程：\n"
                "1. 选择服务器\n"
                "2. 选择系统\n"
                "3. 选择 SSH key\n"
                "4. 选择磁盘方案\n"
                "5. 确认安装"
            )
            return

        servers = ovh_client.list_servers()
        if not servers:
            await update.message.reply_text("❌ 没有服务器")
            return

        target = context.args[0]
        server = None
        if target.isdigit():
            idx = int(target) - 1
            if 0 <= idx < len(servers):
                server = servers[idx]
        else:
            for s in servers:
                if target in s["name"]:
                    server = s
                    break

        if not server:
            await update.message.reply_text("❌ 找不到服务器，用 /servers 查看列表")
            return

        service_name = server["name"]

        # 只有序号 → 列出可用系统
        if len(context.args) == 1:
            msg = await update.message.reply_text(f"⏳ 正在获取可用系统列表...")
            templates = ovh_client.get_server_templates(service_name)
            if not templates:
                await msg.edit_text("❌ 获取系统列表失败")
                return

            os_groups = {}
            for t in templates:
                base = t.split("-")[0].split("_")[0]
                if base not in os_groups:
                    os_groups[base] = []
                os_groups[base].append(t)

            lines = [f"💿 *可用系统* — `{service_name}`\n"]
            for os_name in sorted(os_groups.keys()):
                lines.append(f"*{os_name}:*")
                for t in os_groups[os_name]:
                    lines.append(f"  `{t}`")
                lines.append("")

            lines.append(f"💡 安装系统请使用 /servers 按钮流程\n⚠️ 安装会清除所有数据！")
            text = "\n".join(lines)
            if len(text) > 4000:
                text = text[:3900] + "\n... (已截断)"
            await msg.edit_text(text, parse_mode="Markdown")
            return

        # 有系统名 → 解析选项并确认安装
        template = context.args[1]
        custom_hostname = None
        ssh_key_name = None
        raid0 = False
        raid_disks = None
        disk_group_id = None
        unknown_opts = []
        for opt in context.args[2:]:
            low = opt.lower()
            if low == "raid0":
                raid0 = True
            elif low.startswith("key="):
                ssh_key_name = opt.split("=", 1)[1]
            elif low.startswith("host="):
                custom_hostname = opt.split("=", 1)[1]
            elif low.startswith("disks="):
                try:
                    raid_disks = int(opt.split("=", 1)[1])
                except ValueError:
                    unknown_opts.append(opt)
            elif low.startswith("group="):
                try:
                    disk_group_id = int(opt.split("=", 1)[1])
                except ValueError:
                    unknown_opts.append(opt)
            else:
                unknown_opts.append(opt)

        if unknown_opts:
            await update.message.reply_text(f"❌ 无法识别参数: {' '.join(unknown_opts)}\n请使用 /servers 按钮流程安装系统")
            return

        if ssh_key_name:
            keys = ovh_client.list_ssh_keys()
            if ssh_key_name not in keys:
                await update.message.reply_text(f"❌ OVH SSH 密钥 `{ssh_key_name}` 不存在\n可用密钥: {', '.join(keys) if keys else '无'}", parse_mode="Markdown")
                return

        if raid0 and disk_group_id is None:
            await update.message.reply_text(
                "❌ RAID0 必须显式指定 `group=磁盘组ID`，避免把 SSD 和 HDD 混合组阵列。\n\n"
                "推荐使用 `/servers` 按钮流程，Bot 会读取 OVH 硬件规格并自动生成正确的 RAID0 选项。",
                parse_mode="Markdown"
            )
            return

        action_id = str(int(time.time() * 1000))[-10:]
        pending_actions[action_id] = {
            "type": "reinstall",
            "service_name": service_name,
            "template": template,
            "hostname": custom_hostname,
            "ssh_key_name": ssh_key_name,
            "raid0": raid0,
            "raid_disks": raid_disks,
            "disk_group_id": disk_group_id,
        }
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚠️ 确认安装", callback_data=f"act|{action_id}"),
            InlineKeyboardButton("取消", callback_data="cancel"),
        ]])
        await update.message.reply_text(
            f"⚠️ *确认安装系统*\n\n"
            f"🖥️ 服务器: `{service_name}`\n"
            f"📦 型号: {server.get('commercial_range','?')}\n"
            f"💾 当前系统: {server.get('os','?')}\n"
            f"💿 安装系统: `{template}`\n"
            + (f"🔑 SSH密钥: `{ssh_key_name}`\n" if ssh_key_name else "")
            + (f"🧩 RAID: RAID0 group={disk_group_id}" + (f" ({raid_disks} disks)" if raid_disks else "") + "\n" if raid0 else "")
            + (f"🏷️ 主机名: {custom_hostname}\n" if custom_hostname else "")
            + f"\n🚨 *所有数据将被清除！*", 
            parse_mode="Markdown",
            reply_markup=kb
        )

    async def reboot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """重启服务器"""
        if not check_user(update.effective_user.id):
            return

        if not context.args:
            await update.message.reply_text("用法: /reboot <序号或名称>\n先 /servers 查看列表")
            return

        servers = ovh_client.list_servers()
        if not servers:
            await update.message.reply_text("❌ 没有服务器")
            return

        target = context.args[0]
        server = None
        if target.isdigit():
            idx = int(target) - 1
            if 0 <= idx < len(servers):
                server = servers[idx]
        else:
            for s in servers:
                if target in s["name"]:
                    server = s
                    break

        if not server:
            await update.message.reply_text("❌ 找不到服务器")
            return

        action_id = str(int(time.time() * 1000))[-10:]
        pending_actions[action_id] = {
            "type": "reboot",
            "service_name": server["name"],
        }
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚠️ 确认重启", callback_data=f"act|{action_id}"),
            InlineKeyboardButton("取消", callback_data="cancel"),
        ]])
        await update.message.reply_text(
            f"⚠️ 确认重启 `{server['name']}`?\n\n"
            f"📦 {server.get('commercial_range','?')} | 💻 {server.get('os','?')}",
            parse_mode="Markdown",
            reply_markup=kb
        )

    async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理内联按钮回调 - 支持带存储类型的下单"""
        nonlocal watch_running
        query = update.callback_query
        await query.answer()

        if not check_user(query.from_user.id):
            await query.answer("⛔ 未授权", show_alert=True)
            return

        data = query.data
        parts = data.split("|")

        if parts[0] == "buy" and len(parts) >= 3 and parts[1] == "preset":
            plan_code = resolve_plan_code(parts[2])
            if not plan_code:
                return
            dc = parts[3]
            target_storage = parts[4] if len(parts) > 4 else None
            session_id = str(int(time.time() * 1000))[-10:]
            all_configs = ovh_client.check_availability(plan_code)
            buy_sessions[session_id] = {
                "plan_code": plan_code,
                "all_configs": all_configs,
                "selected_cfg": None,
                "selected_dc": None,
                "target_storage": target_storage,
                "target_memory": None,
                "count": 1,
            }
            # 从 preset 直接带入第一步选择的配置和机房
            selected = None
            for idx, cfg in enumerate(all_configs):
                if cfg["storage"].lower().find(target_storage or "") >= 0:
                    selected = cfg
                    break
            if selected:
                buy_sessions[session_id]["selected_cfg"] = selected
                buy_sessions[session_id]["selected_dc"] = dc
                keyboard = [
                    [InlineKeyboardButton("1 单", callback_data=f"buy|count|{session_id}|1"), InlineKeyboardButton("2 单", callback_data=f"buy|count|{session_id}|2")],
                    [InlineKeyboardButton("3 单", callback_data=f"buy|count|{session_id}|3"), InlineKeyboardButton("5 单", callback_data=f"buy|count|{session_id}|5")],
                    [InlineKeyboardButton("10 单", callback_data=f"buy|count|{session_id}|10"), InlineKeyboardButton("自定义", callback_data=f"buy|count|{session_id}|custom")],
                ]
                await query.edit_message_text(
                    f"🎯 选择下单数量\n\n型号: `{plan_code}`\n配置: {format_memory(selected['memory'])} + {format_storage(selected['storage'])}\n机房: {dc}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return

        elif parts[0] == "orders" and parts[1] == "p":
            # 订单翻页
            page = int(parts[2])
            offset = page * 10
            orders, total = ovh_client.list_recent_orders(offset, 10)

            STATUS_MAP = {
                "delivered": ("✅", "Complete"),
                "delivering": ("🔄", "Being processed"),
                "pendingPayment": ("⏳", "Pending payment"),
                "notPaid": ("⏳", "Not paid"),
                "validatingPayment": ("🔄", "Validating payment"),
                "canceled": ("❌", "Canceled"),
                "expired": ("💀", "Expired"),
            }
            lines = ["📋 *订单列表*（同 OVH 官网）\n"]
            for o in orders:
                date_str = to_bjt(o["date"])[:10] if o.get("date") else "N/A"
                price_str = o.get("price_text") or ""
                emoji, label = STATUS_MAP.get(o["status"], ("📌", o["status"]))
                lines.append(f"{date_str}  `{o['order_id']}`\n   {emoji} {label}  {price_str}\n")

            lines.append(f"\n💡 `/status <订单号>` 查看详情")
            lines.append(f"📄 共 {total} 个订单 — 第 {page+1}/{(total+9)//10} 页")

            keyboard = []
            row = []
            if page > 0:
                row.append(InlineKeyboardButton("◀️ 上一页", callback_data=f"orders|p|{page-1}"))
            if offset + 10 < total:
                row.append(InlineKeyboardButton("▶️ 下一页", callback_data=f"orders|p|{page+1}"))
            if row:
                keyboard.append(row)
            keyboard.append([InlineKeyboardButton("取消", callback_data="cancel")])

            await query.edit_message_text(
                "\n".join(lines),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        elif parts[0] == "srv" and len(parts) >= 3:
            op = parts[1]
            action_id = parts[2]
            action = pending_actions.get(action_id)
            if not action:
                await query.edit_message_text("❌ 操作已过期，请重新 /servers")
                return
            service_name = action["service_name"]

            if op == "install":
                templates = ovh_client.get_server_templates(service_name)
                preferred = [
                    "debian12_64", "debian13_64",
                    "ubuntu2404-server_64", "ubuntu2204-server_64",
                    "proxmox8_64", "proxmox9_64",
                    "rocky9_64", "alma9_64",
                ]
                available = [t for t in preferred if t in templates]
                if not available:
                    available = templates[:8]
                keyboard = []
                for t in available:
                    keyboard.append([InlineKeyboardButton(t, callback_data=f"srv|os|{action_id}|{t}")])
                keyboard.append([InlineKeyboardButton("取消", callback_data="cancel")])
                await query.edit_message_text(
                    f"💿 选择要安装的系统\n\n服务器: {service_name}",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            elif op == "os" and len(parts) >= 4:
                action["template"] = parts[3]
                keys = ovh_client.list_ssh_keys()
                keyboard = []
                for k in keys[:8]:
                    keyboard.append([InlineKeyboardButton(f"🔑 {k}", callback_data=f"srv|key|{action_id}|{k}")])
                keyboard.append([InlineKeyboardButton("不使用 SSH key", callback_data=f"srv|key|{action_id}|none")])
                keyboard.append([
                    InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"srv|install|{action_id}"),
                    InlineKeyboardButton("取消", callback_data="cancel")
                ])
                await query.edit_message_text(
                    f"🔑 选择 SSH 密钥\n\n服务器: {service_name}\n系统: {action['template']}",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            elif op == "key" and len(parts) >= 4:
                action["ssh_key_name"] = None if parts[3] == "none" else parts[3]
                keyboard = [[InlineKeyboardButton("默认分区 / 无 RAID", callback_data=f"srv|raid|{action_id}|none")]]
                for dg in action.get("disk_groups", []):
                    group_id = dg.get("diskGroupId")
                    disks = dg.get("numberOfDisks") or 0
                    if group_id is None or disks < 2:
                        continue
                    size = dg.get("diskSize", {})
                    size_txt = f"{size.get('value','?')}{size.get('unit','')}"
                    disk_type = dg.get("diskType", "DISK")
                    label = f"RAID0 group={group_id} {disks}x {disk_type} {size_txt}"
                    keyboard.append([InlineKeyboardButton(label, callback_data=f"srv|raid|{action_id}|g{group_id}d{disks}")])
                keyboard.append([
                    InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"srv|os|{action_id}|{action['template']}"),
                    InlineKeyboardButton("取消", callback_data="cancel")
                ])
                await query.edit_message_text(
                    f"🧩 选择磁盘方案\n\n服务器: {service_name}\n系统: {action['template']}\nSSH key: {action.get('ssh_key_name') or '不使用'}\n\nRAID0 只会对按钮显示的同一个磁盘组执行，不会混合不同类型磁盘。",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            elif op == "raid" and len(parts) >= 4:
                mode = parts[3]
                if mode.startswith("g") and "d" in mode:
                    try:
                        group_part, disk_part = mode[1:].split("d", 1)
                        group_id = int(group_part)
                        disks = int(disk_part)
                    except ValueError:
                        await query.edit_message_text("❌ 磁盘方案参数无效，请重新 /servers")
                        return
                    dg = next((x for x in action.get("disk_groups", []) if x.get("diskGroupId") == group_id), None)
                    disk_type = dg.get("diskType", "DISK") if dg else "DISK"
                    size = dg.get("diskSize", {}) if dg else {}
                    size_txt = f"{size.get('value','?')}{size.get('unit','')}"
                    action["raid0"] = True
                    action["disk_group_id"] = group_id
                    action["raid_disks"] = disks
                    raid_text = f"RAID0 group={group_id} {disks}x {disk_type} {size_txt}"
                else:
                    action["raid0"] = False
                    action["disk_group_id"] = None
                    action["raid_disks"] = None
                    raid_text = "默认分区 / 无 RAID"

                confirm_id = str(int(time.time() * 1000))[-10:]
                pending_actions[confirm_id] = {
                    "type": "reinstall",
                    "service_name": service_name,
                    "template": action["template"],
                    "hostname": None,
                    "ssh_key_name": action.get("ssh_key_name"),
                    "raid0": action.get("raid0", False),
                    "raid_disks": action.get("raid_disks"),
                    "disk_group_id": action.get("disk_group_id"),
                }
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚠️ 确认安装", callback_data=f"act|{confirm_id}")],
                    [InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"srv|key|{action_id}|{action.get('ssh_key_name') or 'none'}"), InlineKeyboardButton("取消", callback_data="cancel")],
                ])
                await query.edit_message_text(
                    f"⚠️ 确认安装系统\n\n"
                    f"服务器: {service_name}\n"
                    f"系统: {action['template']}\n"
                    f"SSH key: {action.get('ssh_key_name') or '不使用'}\n"
                    f"磁盘: {raid_text}\n\n"
                    f"🚨 所有数据将被清除！",
                    reply_markup=keyboard
                )

            elif op == "reboot":
                confirm_id = str(int(time.time() * 1000))[-10:]
                pending_actions[confirm_id] = {"type": "reboot", "service_name": service_name}
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚠️ 确认重启", callback_data=f"act|{confirm_id}"),
                    InlineKeyboardButton("取消", callback_data="cancel"),
                ]])
                await query.edit_message_text(f"⚠️ 确认重启 {service_name}?", reply_markup=kb)

        elif parts[0] == "watch" and len(parts) >= 3:
            stage = parts[1]
            session_id = parts[2]
            session = watch_sessions.get(session_id)
            if not session:
                await query.edit_message_text("❌ 监控会话已过期，请重新 /watch")
                return

            plan_code = session["plan_code"]
            all_configs = session["all_configs"]
            display_configs = session.get("display_configs", all_configs)

            if stage == "cfgback":
                buttons = []
                for idx, cfg in enumerate(display_configs[:20]):
                    buttons.append([InlineKeyboardButton(
                        f"#{idx+1} {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}",
                        callback_data=f"watch|cfg|{session_id}|{idx}"
                    )])
                buttons.append([InlineKeyboardButton("取消", callback_data="cancel")])
                await query.edit_message_text(
                    f"📡 *选择要监控的配置*\n\n型号: `{plan_code}`",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )

            elif stage == "dcback":
                cfg = session.get("selected_cfg")
                if not cfg:
                    await query.edit_message_text("❌ 会话状态丢失，请重新 /watch")
                    return
                dcs = list(cfg["datacenters"].items())
                keyboard = []
                keyboard.append([InlineKeyboardButton("🌐 全部机房", callback_data=f"watch|dc|{session_id}|all")])
                for dc, status in dcs:
                    status_cn = format_dc_status(status)
                    keyboard.append([InlineKeyboardButton(f"{format_dc(dc)} ({status_cn})", callback_data=f"watch|dc|{session_id}|{dc}")])
                keyboard.append([
                    InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"watch|cfgback|{session_id}"),
                    InlineKeyboardButton("取消", callback_data="cancel")
                ])
                await query.edit_message_text(
                    f"📍 选择机房\n\n型号: `{plan_code}`\n配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            elif stage == "countback":
                cfg = session.get("selected_cfg")
                dc = session.get("selected_dc")
                if not cfg:
                    await query.edit_message_text("❌ 会话状态丢失，请重新 /watch")
                    return
                dc_display = "全部机房" if dc is None else format_dc(dc)
                keyboard = [
                    [InlineKeyboardButton("1 单", callback_data=f"watch|count|{session_id}|1"), InlineKeyboardButton("2 单", callback_data=f"watch|count|{session_id}|2")],
                    [InlineKeyboardButton("3 单", callback_data=f"watch|count|{session_id}|3"), InlineKeyboardButton("5 单", callback_data=f"watch|count|{session_id}|5")],
                    [InlineKeyboardButton("10 单", callback_data=f"watch|count|{session_id}|10"), InlineKeyboardButton("自定义", callback_data=f"watch|count|{session_id}|custom")],
                    [InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"watch|dcback|{session_id}"), InlineKeyboardButton("取消", callback_data="cancel")],
                ]
                await query.edit_message_text(
                    f"🎯 选择下单数量\n\n型号: `{plan_code}`\n配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}\n机房: {dc_display}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            elif stage == "cfg" and len(parts) >= 4:
                idx = int(parts[3])
                if idx < 0 or idx >= len(display_configs):
                    await query.edit_message_text("❌ 配置已过期，请重新 /watch")
                    return
                cfg = display_configs[idx]
                session["selected_fqn"] = cfg["fqn"]
                session["selected_cfg"] = cfg

                dcs = list(cfg["datacenters"].items())
                keyboard = []
                keyboard.append([InlineKeyboardButton("🌐 全部机房", callback_data=f"watch|dc|{session_id}|all")])
                for dc, status in dcs:
                    status_cn = format_dc_status(status)
                    keyboard.append([InlineKeyboardButton(f"{format_dc(dc)} ({status_cn})", callback_data=f"watch|dc|{session_id}|{dc}")])
                keyboard.append([
                    InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"watch|cfgback|{session_id}"),
                    InlineKeyboardButton("取消", callback_data="cancel")
                ])
                title = f"📍 选择机房\n\n型号: `{plan_code}`\n配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}"
                if not dcs:
                    title = f"📍 这个配置没有可选机房\n\n型号: `{plan_code}`\n配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}"
                await query.edit_message_text(
                    title,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            elif stage == "dc" and len(parts) >= 4:
                dc = parts[3]
                cfg = session.get("selected_cfg")
                if not cfg:
                    await query.edit_message_text("❌ 会话状态丢失，请重新 /watch")
                    return
                if dc == "all":
                    session["selected_dc"] = None
                else:
                    session["selected_dc"] = dc
                dc_display = "全部机房" if dc == "all" else format_dc(dc)

                keyboard = [
                    [InlineKeyboardButton("1 单", callback_data=f"watch|count|{session_id}|1"), InlineKeyboardButton("2 单", callback_data=f"watch|count|{session_id}|2")],
                    [InlineKeyboardButton("3 单", callback_data=f"watch|count|{session_id}|3"), InlineKeyboardButton("5 单", callback_data=f"watch|count|{session_id}|5")],
                    [InlineKeyboardButton("10 单", callback_data=f"watch|count|{session_id}|10"), InlineKeyboardButton("自定义", callback_data=f"watch|count|{session_id}|custom")],
                    [InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"watch|dcback|{session_id}"), InlineKeyboardButton("取消", callback_data="cancel")],
                ]
                await query.edit_message_text(
                    f"🎯 选择下单数量\n\n"
                    f"型号: `{plan_code}`\n"
                    f"配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}\n"
                    f"机房: {dc_display}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            elif stage == "count" and len(parts) >= 4:
                val = parts[3]
                session["max_orders"] = 1 if val == "custom" else int(val)
                cfg = session.get("selected_cfg")
                dc = session.get("selected_dc")
                if not cfg:
                    await query.edit_message_text("❌ 会话状态丢失，请重新 /watch")
                    return
                dc_display = "全部机房" if dc is None else format_dc(dc)
                confirm_id = str(int(time.time() * 1000))[-10:]
                pending_actions[confirm_id] = {
                    "type": "watch_start",
                    "plan_code": plan_code,
                    "fqn": cfg["fqn"],
                    "dc": dc,
                    "storage": cfg.get("storage"),
                    "memory": cfg.get("memory"),
                    "max_orders": session.get("max_orders", 1),
                }
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("▶️ 确认开始监控", callback_data=f"act|{confirm_id}")],
                    [InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"watch|countback|{session_id}"), InlineKeyboardButton("取消", callback_data="cancel")],
                ])
                await query.edit_message_text(
                    f"📡 确认开始监控\n\n"
                    f"型号: `{plan_code}`\n"
                    f"配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}\n"
                    f"机房: {dc_display}\n"
                    f"下单上限: {session.get('max_orders', 1)}",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )

        elif parts[0] == "watchlist":
            if len(parts) >= 2 and parts[1] == "manage":
                if not watch_tasks:
                    await query.edit_message_text("📭 当前没有监控任务")
                    return
                keyboard = []
                for pc, task in watch_tasks.items():
                    status_icon = "🟢" if task.get("active") else "🔴"
                    keyboard.append([
                        InlineKeyboardButton(
                            f"{status_icon} {pc} ({task.get('ordered', 0)}/{task.get('max_orders', 1)})",
                            callback_data=f"watchlist|task|{pc}"
                        )
                    ])
                keyboard.append([InlineKeyboardButton("取消", callback_data="cancel")])
                await query.edit_message_text(
                    "⚙️ 选择要管理的监控任务",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return

            if len(parts) >= 3 and parts[1] == "task":
                plan_code = parts[2]
                task = watch_tasks.get(plan_code)
                if not task:
                    await query.edit_message_text("❌ 监控任务不存在或已删除")
                    return
                status = "🟢 监控中" if task.get("active") else "🔴 已暂停"
                filter_parts = []
                if task.get("dc"):
                    filter_parts.append(f"机房={format_dc(task['dc'])}")
                else:
                    filter_parts.append("机房=全部机房")
                if task.get("storage"):
                    filter_parts.append(f"存储={format_storage(task['storage'])}")
                if task.get("memory"):
                    filter_parts.append(f"内存={format_memory(task['memory'])}")
                action_btn = InlineKeyboardButton(
                    "⏸ 暂停监控" if task.get("active") else "▶️ 启用监控",
                    callback_data=f"watchlist|toggle|{plan_code}"
                )
                keyboard = InlineKeyboardMarkup([
                    [action_btn],
                    [InlineKeyboardButton("🗑 删除监控", callback_data=f"watchlist|delete|{plan_code}")],
                    [InlineKeyboardButton("⬅️ 返回任务列表", callback_data="watchlist|manage"), InlineKeyboardButton("取消", callback_data="cancel")],
                ])
                await query.edit_message_text(
                    f"⚙️ 管理监控任务\n\n"
                    f"{status} `{plan_code}`\n"
                    f"条件: {', '.join(filter_parts)}\n"
                    f"进度: {task.get('ordered', 0)}/{task.get('max_orders', 1)} 单",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
                return

            if len(parts) >= 3 and parts[1] == "toggle":
                plan_code = parts[2]
                task = watch_tasks.get(plan_code)
                if not task:
                    await query.edit_message_text("❌ 监控任务不存在或已删除")
                    return
                was_active = task.get("active", True)
                task["active"] = not was_active
                task["chat_id"] = str(query.message.chat_id)
                if task["active"] and task.get("ordered", 0) >= task.get("max_orders", 1):
                    task["ordered"] = 0
                    task["_last_order_time"] = {}
                save_watch_tasks()
                if task["active"] and not watch_running:
                    watch_running = True
                    asyncio.ensure_future(watch_monitor_loop())
                status_text = "已启用" if task["active"] else "已暂停"
                await query.answer(f"{plan_code} {status_text}")
                parts = ["watchlist", "task", plan_code]
                task = watch_tasks.get(plan_code)
                status = "🟢 监控中" if task.get("active") else "🔴 已暂停"
                filter_parts = []
                if task.get("dc"):
                    filter_parts.append(f"机房={format_dc(task['dc'])}")
                else:
                    filter_parts.append("机房=全部机房")
                if task.get("storage"):
                    filter_parts.append(f"存储={format_storage(task['storage'])}")
                if task.get("memory"):
                    filter_parts.append(f"内存={format_memory(task['memory'])}")
                action_btn = InlineKeyboardButton(
                    "⏸ 暂停监控" if task.get("active") else "▶️ 启用监控",
                    callback_data=f"watchlist|toggle|{plan_code}"
                )
                keyboard = InlineKeyboardMarkup([
                    [action_btn],
                    [InlineKeyboardButton("🗑 删除监控", callback_data=f"watchlist|delete|{plan_code}")],
                    [InlineKeyboardButton("⬅️ 返回任务列表", callback_data="watchlist|manage"), InlineKeyboardButton("取消", callback_data="cancel")],
                ])
                await query.edit_message_text(
                    f"⚙️ 管理监控任务\n\n"
                    f"{status} `{plan_code}`\n"
                    f"条件: {', '.join(filter_parts)}\n"
                    f"进度: {task.get('ordered', 0)}/{task.get('max_orders', 1)} 单",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
                return

            if len(parts) >= 3 and parts[1] == "delete":
                plan_code = parts[2]
                task = watch_tasks.get(plan_code)
                if not task:
                    await query.edit_message_text("❌ 监控任务不存在或已删除")
                    return
                task["active"] = False
                del watch_tasks[plan_code]
                save_watch_tasks()
                await query.answer(f"已删除 {plan_code}")
                if not watch_tasks:
                    await query.edit_message_text("📭 当前没有监控任务")
                    return
                keyboard = []
                for pc, t in watch_tasks.items():
                    status_icon = "🟢" if t.get("active") else "🔴"
                    keyboard.append([
                        InlineKeyboardButton(
                            f"{status_icon} {pc} ({t.get('ordered', 0)}/{t.get('max_orders', 1)})",
                            callback_data=f"watchlist|task|{pc}"
                        )
                    ])
                keyboard.append([InlineKeyboardButton("取消", callback_data="cancel")])
                await query.edit_message_text(
                    "⚙️ 选择要管理的监控任务",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return

        elif parts[0] == "buy" and len(parts) >= 3:
            stage = parts[1]
            session_id = parts[2]
            session = buy_sessions.get(session_id)
            if not session:
                await query.edit_message_text("❌ 抢购会话已过期，请重新 /buy")
                return

            plan_code = session["plan_code"]
            all_configs = session["all_configs"]
            display_configs = session.get("display_configs", all_configs)

            if stage == "cfgback":
                buttons = []
                for idx, cfg in enumerate(display_configs[:20]):
                    buttons.append([InlineKeyboardButton(
                        f"#{idx+1} {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}",
                        callback_data=f"buy|cfg|{session_id}|{idx}"
                    )])
                buttons.append([InlineKeyboardButton("取消", callback_data="cancel")])
                await query.edit_message_text(
                    f"🛒 *选择要抢购的配置*\n\n型号: `{plan_code}`",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )

            elif stage == "dcback":
                cfg = session.get("selected_cfg")
                if not cfg:
                    await query.edit_message_text("❌ 会话状态丢失，请重新 /buy")
                    return
                dcs = [(dc, status) for dc, status in cfg["datacenters"].items() if status not in UNAVAILABLE_STATES]
                if not dcs:
                    await query.edit_message_text("❌ 这个配置当前已无货，请重新 /buy 查询最新库存")
                    return
                keyboard = []
                for dc, status in dcs:
                    status_cn = format_dc_status(status)
                    keyboard.append([InlineKeyboardButton(f"{format_dc(dc)} ({status_cn})", callback_data=f"buy|dc|{session_id}|{dc}")])
                keyboard.append([
                    InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"buy|cfgback|{session_id}"),
                    InlineKeyboardButton("取消", callback_data="cancel")
                ])
                await query.edit_message_text(
                    f"📍 选择机房\n\n型号: `{plan_code}`\n配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            elif stage == "countback":
                cfg = session.get("selected_cfg")
                dc = session.get("selected_dc")
                if not cfg or not dc:
                    await query.edit_message_text("❌ 会话状态丢失，请重新 /buy")
                    return
                dc_display = format_dc(dc)
                keyboard = [
                    [InlineKeyboardButton("1 单", callback_data=f"buy|count|{session_id}|1"), InlineKeyboardButton("2 单", callback_data=f"buy|count|{session_id}|2")],
                    [InlineKeyboardButton("3 单", callback_data=f"buy|count|{session_id}|3"), InlineKeyboardButton("5 单", callback_data=f"buy|count|{session_id}|5")],
                    [InlineKeyboardButton("10 单", callback_data=f"buy|count|{session_id}|10"), InlineKeyboardButton("自定义", callback_data=f"buy|count|{session_id}|custom")],
                    [InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"buy|dcback|{session_id}"), InlineKeyboardButton("取消", callback_data="cancel")],
                ]
                await query.edit_message_text(
                    f"🎯 选择下单数量\n\n型号: `{plan_code}`\n配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}\n机房: {dc_display}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            elif stage == "cfg" and len(parts) >= 4:
                idx = int(parts[3])
                if idx < 0 or idx >= len(display_configs):
                    await query.edit_message_text("❌ 配置已过期，请重新 /buy")
                    return
                cfg = display_configs[idx]
                session["selected_cfg"] = cfg

                dcs = [(dc, status) for dc, status in cfg["datacenters"].items() if status not in UNAVAILABLE_STATES]
                if not dcs:
                    await query.edit_message_text("❌ 这个配置当前已无货，请重新 /buy 查询最新库存")
                    return
                keyboard = []
                for dc, status in dcs:
                    status_cn = format_dc_status(status)
                    keyboard.append([InlineKeyboardButton(f"{format_dc(dc)} ({status_cn})", callback_data=f"buy|dc|{session_id}|{dc}")])
                keyboard.append([
                    InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"buy|cfgback|{session_id}"),
                    InlineKeyboardButton("取消", callback_data="cancel")
                ])
                title = f"📍 选择机房\n\n型号: `{plan_code}`\n配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}"
                if not dcs:
                    title = f"📍 这个配置没有可选机房\n\n型号: `{plan_code}`\n配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}"
                await query.edit_message_text(
                    title,
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            elif stage == "dc" and len(parts) >= 4:
                dc = parts[3]
                cfg = session.get("selected_cfg")
                if not cfg:
                    await query.edit_message_text("❌ 会话状态丢失，请先选择配置")
                    return
                session["selected_dc"] = dc
                dc_display = format_dc(dc)

                keyboard = [
                    [InlineKeyboardButton("1 单", callback_data=f"buy|count|{session_id}|1"), InlineKeyboardButton("2 单", callback_data=f"buy|count|{session_id}|2")],
                    [InlineKeyboardButton("3 单", callback_data=f"buy|count|{session_id}|3"), InlineKeyboardButton("5 单", callback_data=f"buy|count|{session_id}|5")],
                    [InlineKeyboardButton("10 单", callback_data=f"buy|count|{session_id}|10"), InlineKeyboardButton("自定义", callback_data=f"buy|count|{session_id}|custom")],
                    [InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"buy|dcback|{session_id}"), InlineKeyboardButton("取消", callback_data="cancel")],
                ]
                await query.edit_message_text(
                    f"🎯 选择下单数量\n\n"
                    f"型号: `{plan_code}`\n"
                    f"配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}\n"
                    f"机房: {dc_display}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            elif stage == "count" and len(parts) >= 4:
                val = parts[3]
                session["count"] = 1 if val == "custom" else int(val)
                cfg = session.get("selected_cfg")
                dc = session.get("selected_dc")
                if not cfg or not dc:
                    await query.edit_message_text("❌ 会话状态丢失，请先选择机房")
                    return
                dc_display = format_dc(dc)
                confirm_id = str(int(time.time() * 1000))[-10:]
                pending_actions[confirm_id] = {
                    "type": "buy_start",
                    "plan_code": plan_code,
                    "fqn": cfg["fqn"],
                    "dc": dc,
                    "storage": cfg.get("storage"),
                    "memory": cfg.get("memory"),
                    "count": session.get("count", 1),
                }
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛒 确认开始抢购", callback_data=f"act|{confirm_id}")],
                    [InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"buy|countback|{session_id}"), InlineKeyboardButton("取消", callback_data="cancel")],
                ])
                await query.edit_message_text(
                    f"🛒 确认开始抢购\n\n"
                    f"型号: `{plan_code}`\n"
                    f"配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}\n"
                    f"机房: {dc_display}\n"
                    f"下单数量: {session.get('count', 1)}",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )

        elif parts[0] == "act" and len(parts) >= 2:
            action_id = parts[1]
            action = pending_actions.get(action_id)
            if not action:
                await query.edit_message_text("❌ 操作已过期，请重新发起")
                return

            if action["type"] == "buy_start":
                plan_code = action["plan_code"]
                server_type = guess_server_type(plan_code)
                dc = action.get("dc")

                # 先检查有没有货，没货就不浪费时间调用下单 API
                available = ovh_client.find_available_configs(
                    plan_code,
                    target_dc=dc,
                    target_storage=action.get("storage"),
                    target_memory=action.get("memory"),
                )
                if not available:
                    pending_actions.pop(action_id, None)
                    dc_display = format_dc(dc) if dc else "全部机房"
                    cfg_mem = format_memory(action.get("memory", ""))
                    cfg_stor = format_storage(action.get("storage", ""))
                    await query.edit_message_text(
                        f"❌ *当前无货，无法抢购*\n\n"
                        f"📦 型号: `{plan_code}`\n"
                        f"💾 配置: {cfg_mem} + {cfg_stor}\n"
                        f"📍 机房: {dc_display}\n\n"
                        f"💡 请用 /watch 设定监控，等有货后自动下单",
                        parse_mode="Markdown"
                    )
                    return

                dc_display = format_dc(dc) if dc else available[0]["datacenter"]
                await query.edit_message_text(f"🚀 正在抢购 `{plan_code}` @ {dc_display}...")
                result = ovh_client.quick_buy(
                    plan_code=plan_code,
                    server_type=server_type,
                    datacenter=dc,
                    target_storage=action.get("storage"),
                    target_memory=action.get("memory"),
                )
                text = _format_buy_result(result)
                if result.get("success"):
                    pending_actions.pop(action_id, None)
                if action.get("count", 1) > 1 and result.get("success"):
                    text += f"\n\n📊 已按按钮下单数: {action['count']}"
                await query.edit_message_text(text, parse_mode="Markdown")

            elif action["type"] == "watch_start":
                try:
                    plan_code = action["plan_code"]
                    watch_tasks[plan_code] = {
                        "dc": action.get("dc"),
                        "storage": action.get("storage"),
                        "memory": action.get("memory"),
                        "max_orders": action.get("max_orders", 1),
                        "ordered": 0,
                        "active": True,
                        "chat_id": str(query.message.chat_id),
                        "_last_order_time": {},
                    }
                    save_watch_tasks()
                    if not watch_running:
                        watch_running = True
                        asyncio.ensure_future(watch_monitor_loop())
                    pending_actions.pop(action_id, None)
                    await query.edit_message_text(
                        f"📡 *开始监控* `{plan_code}`\n\n"
                        f"📍 机房: {format_dc(action.get('dc')) if action.get('dc') else '全部机房'}\n"
                        f"📦 配置: {format_memory(action.get('memory'))} + {format_storage(action.get('storage'))}\n"
                        f"🎯 下单上限: {action.get('max_orders', 1)}\n"
                        f"📊 已下: 0 单\n\n"
                        f"💡 达到上限后自动停止",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"启动监控失败: {e}\n{traceback.format_exc()}")
                    await query.edit_message_text(
                        f"❌ 启动监控失败，操作未过期，可重试确认按钮或重新 /watch\n\n`{e}`",
                        parse_mode="Markdown"
                    )

            elif action["type"] == "reinstall":
                service_name = action["service_name"]
                template = action["template"]
                hostname = action.get("hostname")
                ssh_key_name = action.get("ssh_key_name")
                raid0 = action.get("raid0", False)
                raid_disks = action.get("raid_disks")
                disk_group_id = action.get("disk_group_id")
                await query.edit_message_text(f"⏳ 正在安装 `{template}` 到 `{service_name}`...")
                try:
                    result = ovh_client.reinstall_server(
                        service_name, template, hostname,
                        ssh_key_name=ssh_key_name, raid0=raid0,
                        raid_disks=raid_disks, disk_group_id=disk_group_id
                    )
                    task_id = result.get("taskId", "?") if isinstance(result, dict) else "?"
                    pending_actions.pop(action_id, None)
                    raid_text = None
                    if raid0:
                        raid_text = f"RAID0 group={disk_group_id}" + (f" disks={raid_disks}" if raid_disks else "")
                    else:
                        raid_text = "默认分区 / 无 RAID"
                    await query.edit_message_text(
                        f"💿 *系统安装进度*\n\n"
                        f"🖥️ 服务器: `{service_name}`\n"
                        f"💿 系统: `{template}`\n"
                        + (f"🔑 SSH密钥: `{ssh_key_name}`\n" if ssh_key_name else "")
                        + f"🧩 磁盘: `{raid_text}`\n"
                        + f"📋 任务ID: `{task_id}`\n\n"
                        f"`█░░░░░░░░░░░` 5%\n"
                        f"📌 状态: `安装任务已提交`\n"
                        f"⏱️ 耗时: 0分0秒\n\n"
                        f"⏳ Bot 会自动刷新此进度。",
                        parse_mode="Markdown"
                    )
                    asyncio.ensure_future(
                        track_install_progress(query.message, service_name, template, str(task_id), ssh_key_name, raid_text)
                    )
                except Exception as e:
                    await query.edit_message_text(f"❌ 安装失败: {e}")

            elif action["type"] == "reboot":
                service_name = action["service_name"]
                await query.edit_message_text(f"⏳ 正在重启 `{service_name}`...")
                try:
                    ovh_client.reboot_server(service_name)
                    pending_actions.pop(action_id, None)
                    await query.edit_message_text(
                        f"✅ 重启指令已发送\n\n🖥️ `{service_name}`\n⏳ 服务器正在重启...",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    await query.edit_message_text(f"❌ 重启失败: {e}")

        elif parts[0] == "cancel":
            try:
                await query.message.delete()
            except Exception as e:
                logger.error(f"删除取消消息失败: {e}")
                await query.edit_message_reply_markup(reply_markup=None)

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理转发的消息，自动解析服务器信息并下单（支持存储类型识别）"""
        if not check_user(update.effective_user.id):
            return

        text = update.message.text or ""
        if not text.strip():
            return

        # 解析 planCode
        plan_code = parse_plan_code(text)
        if not plan_code:
            # 尝试特殊型号名映射
            known_plans = {
                "ks-1-b": "26sk10b-v1", "ks1b": "26sk10b-v1",
                "ks-5-a": "26sk50a-v1", "ks5a": "26sk50a-v1",
                "ks-5-b": "26sk50b-v1", "ks5b": "26sk50b-v1",
            }
            for name, pc in known_plans.items():
                if name in text.lower():
                    plan_code = pc
                    break

        if not plan_code:
            return

        # 解析数据中心
        dc = parse_datacenter(text)

        # 解析存储类型
        target_storage = None
        text_lower = text.lower()
        if "nvme" in text_lower:
            m = re.search(r'(\d+x\d+)\s*gb?\s*nvme', text_lower.replace(" ", ""))
            if m:
                target_storage = m.group(1).replace("gb", "") + "nvme"
            else:
                target_storage = "nvme"
        elif any(kw in text_lower for kw in ["hdd", "sas", "sata", "硬盘"]):
            m = re.search(r'(\d+x\d+)\s*(?:tb|gb)?\s*(?:hdd|sas|sata|硬盘)', text_lower.replace(" ", ""))
            if m:
                target_storage = m.group(1) + "hdd"
            else:
                target_storage = "hdd"

        server_type = guess_server_type(plan_code)
        filter_parts = []
        if dc:
            filter_parts.append(f"机房={dc}")
        if target_storage:
            filter_parts.append(f"存储={target_storage}")
        filter_str = f" ({', '.join(filter_parts)})" if filter_parts else ""

        msg = await update.message.reply_text(
            f"🔍 识别到: `{plan_code}`{filter_str}\n🚀 正在下单...",
            parse_mode="Markdown",
        )

        result = ovh_client.quick_buy(
            plan_code=plan_code,
            server_type=server_type,
            datacenter=dc,
            target_storage=target_storage,
        )

        reply_text = _format_buy_result(result)
        await msg.edit_text(reply_text, parse_mode="Markdown")

    def _format_buy_result(result: dict) -> str:
        if result["success"]:
            text = "✅ *抢购成功！*\n\n"
            text += f"📦 服务器: `{result['plan_code']}`\n"
            text += f"🏗️ 数据中心: {format_dc(result['datacenter'])}\n"

            if result.get("config_info"):
                ci = result["config_info"]
                text += f"💾 内存: {ci['memory_display']}\n"
                text += f"💿 存储: {ci['storage_display']}\n"

            text += f"🛒 购物车: `{result['cart_id']}`\n"

            if result.get("price"):
                p = result["price"]
                text += f"💰 价格: {p.get('withTax', '?')} {p.get('currencyCode', 'EUR')}\n"

            if result["order_id"]:
                text += f"📋 订单号: `{result['order_id']}`\n"
            if result["payment_url"]:
                text += f"💳 付款链接: {result['payment_url']}\n"

            text += f"\n⏱️ 耗时: {result['elapsed']}s"

            if result["order_id"]:
                text += "\n\n⚠️ *请尽快手动付款以锁定订单！*"
            else:
                text += f"\n\n⚠️ 购物车已创建，请使用 /order {result['cart_id']} 生成订单"
        else:
            text = "❌ *抢购失败*\n\n"
            text += f"📦 服务器: `{result['plan_code']}`\n"
            text += f"❗ 错误: {result['error']}\n"
            text += f"⏱️ 耗时: {result['elapsed']}s"

            # 如果有所有配置信息，显示
            if result.get("all_configs"):
                text += "\n\n📊 *所有配置状态:*\n"
                for cfg in result["all_configs"]:
                    mem = format_memory(cfg["memory"])
                    stor = format_storage(cfg["storage"])
                    text += f"  {mem} + {stor}:\n"
                    for dc, status in cfg["datacenters"].items():
                        icon = "✅" if status not in UNAVAILABLE_STATES else "❌"
                        text += f"    {icon} {format_dc(dc)}: {format_dc_status(status)}\n"

        return text

    # ---- 构建 Bot ----
    app = ApplicationBuilder().token(bot_token).build()
    bot_app = app

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("buy", buy_cmd))
    app.add_handler(CommandHandler("eco", buy_cmd))
    app.add_handler(CommandHandler("dedi", buy_cmd))
    app.add_handler(CommandHandler("dedicated", buy_cmd))
    app.add_handler(CommandHandler("check", check_cmd))
    app.add_handler(CommandHandler("catalog", catalog_cmd))
    app.add_handler(CommandHandler("pay", pay_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("watchlist", watchlist_cmd))
    app.add_handler(CommandHandler("servers", servers_cmd))
    app.add_handler(CommandHandler("keys", keys_cmd))
    app.add_handler(CommandHandler("reinstall", reinstall_cmd))
    app.add_handler(CommandHandler("reboot", reboot_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # 如果有恢复的监控任务，自动启动监控循环
    if watch_tasks:
        active_count = sum(1 for t in watch_tasks.values() if t.get("active"))
        if active_count > 0:
            watch_running = True
            asyncio.ensure_future(watch_monitor_loop())
            logger.info(f"恢复 {active_count} 个监控任务，自动启动监控循环")

    logger.info(f"🤖 OVH 抢购 Bot v2 启动 (区域: {ovh_client.zone}/{ovh_client.subsidiary})")
    app.run_polling()


# ============================================================
# CLI 模式
# ============================================================
def run_cli(cfg: dict):
    import argparse

    parser = argparse.ArgumentParser(description="OVH 服务器抢购工具 v2")
    subparsers = parser.add_subparsers(dest="command")

    buy_p = subparsers.add_parser("buy", help="抢购服务器")
    buy_p.add_argument("plan_code", help="服务器 planCode")
    buy_p.add_argument("--type", choices=["eco", "dedicated"], default="eco")
    buy_p.add_argument("--dc", help="数据中心")
    buy_p.add_argument("--os", help="操作系统")
    buy_p.add_argument("--options", nargs="*", help="硬件选项列表")

    check_p = subparsers.add_parser("check", help="查看所有配置可用性")
    check_p.add_argument("plan_code", help="服务器 planCode")

    catalog_p = subparsers.add_parser("catalog", help="查看服务器目录")
    catalog_p.add_argument("--category", default="eco", help="类别")

    pay_p = subparsers.add_parser("pay", help="获取付款链接")
    pay_p.add_argument("order_id", type=int)

    status_p = subparsers.add_parser("status", help="查看订单状态")
    status_p.add_argument("order_id", type=int)

    subparsers.add_parser("bot", help="启动 Telegram Bot")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    client = OVHClient(cfg)

    if args.command == "buy":
        print(f"🚀 正在抢购 {args.plan_code}...")
        result = client.quick_buy(
            plan_code=args.plan_code,
            server_type=args.type,
            datacenter=args.dc,
            os_name=args.os,
            options=args.options,
        )
        if result["success"]:
            print(f"✅ 抢购成功！")
            print(f"   数据中心: {result['datacenter']}")
            if result.get("config_info"):
                ci = result["config_info"]
                print(f"   内存: {ci['memory_display']}")
                print(f"   存储: {ci['storage_display']}")
            print(f"   购物车: {result['cart_id']}")
            if result["order_id"]:
                print(f"   订单号: {result['order_id']}")
            if result["payment_url"]:
                print(f"   付款链接: {result['payment_url']}")
            if result.get("price"):
                p = result["price"]
                print(f"   价格: {p.get('withTax', '?')} {p.get('currencyCode', 'EUR')}")
            print(f"   耗时: {result['elapsed']}s")
        else:
            print(f"❌ 抢购失败: {result['error']}")
            print(f"   耗时: {result['elapsed']}s")
            if result.get("all_configs"):
                print(f"\n📊 所有配置状态:")
                for c in result["all_configs"]:
                    mem = format_memory(c["memory"])
                    stor = format_storage(c["storage"])
                    print(f"  {mem} + {stor}:")
                    for dc, status in c["datacenters"].items():
                        icon = "✅" if status not in UNAVAILABLE_STATES else "❌"
                        print(f"    {icon} {dc}: {status}")

    elif args.command == "check":
        print(f"🔍 检查 {args.plan_code} 所有配置可用性...")
        all_configs = client.check_availability(args.plan_code)
        if not all_configs:
            print("❌ 未获取到可用性数据")
            return
        for cfg in all_configs:
            mem = format_memory(cfg["memory"])
            stor = format_storage(cfg["storage"])
            print(f"\n  📦 {mem} + {stor}")
            for dc, status in cfg["datacenters"].items():
                icon = "✅" if status not in UNAVAILABLE_STATES else "❌"
                dc_disp = DC_DISPLAY_MAP.get(dc, dc)
                print(f"    {icon} {dc_disp}: {status}")

    elif args.command == "catalog":
        print(f"📖 获取 {args.category} 目录...")
        catalog = client.get_catalog(args.category)
        plans = catalog.get("plans", [])
        for plan in plans:
            pc = plan.get("planCode", "?")
            name = plan.get("invoiceName", "")
            print(f"  {pc} - {name}")

    elif args.command == "pay":
        try:
            url = client.get_payment_url(args.order_id)
            print(f"💳 订单 {args.order_id} 付款链接:\n   {url}")
        except Exception as e:
            print(f"❌ 获取付款链接失败: {e}")

    elif args.command == "status":
        try:
            order = client.get_order(args.order_id)
            status = client.get_order_status(args.order_id)
            print(f"📋 订单 {args.order_id}")
            print(f"   状态: {status}")
            print(f"   日期: {to_bjt(order.get('date', 'N/A'))}")
        except Exception as e:
            print(f"❌ 查询失败: {e}")

    elif args.command == "bot":
        run_bot(cfg)


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    cfg = load_config()

    if len(sys.argv) == 1 and cfg.get("telegram", {}).get("bot_token"):
        print("🤖 启动 Telegram Bot 模式...")
        run_bot(cfg)
    else:
        run_cli(cfg)
