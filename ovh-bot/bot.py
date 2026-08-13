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
import sqlite3
import ipaddress
import sys
import time
import traceback
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

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
    """使用标准 TOML 解析器读取配置，避免注释、转义和数组被误解析。"""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"配置文件格式错误: {path}: {exc}") from exc


def _parse_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"无效的布尔值: {value}")


def is_user_allowed(user_id: int, allowed_users: list, allow_all_users: bool = False) -> bool:
    """默认拒绝访问；只有白名单或显式开放模式才放行。"""
    return allow_all_users or user_id in allowed_users


def execute_buy_batch(client, count: int, **buy_kwargs) -> list:
    """按顺序执行 1-10 次下单，首次失败后停止，避免继续创建无效购物车。"""
    safe_count = max(1, min(int(count), 10))
    results = []
    for _ in range(safe_count):
        result = client.quick_buy(**buy_kwargs)
        results.append(result)
        if not result.get("success"):
            break
    return results


async def run_ovh_call(func, *args, timeout: float = 20, **kwargs):
    """在线程中执行 OVH 请求，并将无限等待转换为可恢复的超时错误。"""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(func, *args, **kwargs),
            timeout=max(0.001, float(timeout)),
        )
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"OVH API {timeout} 秒无响应") from exc


async def run_ovh_call_with_heartbeat(
    func,
    *args,
    timeout: float = 20,
    heartbeat: float = 5,
    on_wait=None,
    **kwargs,
):
    """执行 OVH 请求，并在等待期间定时回报已等待秒数。"""
    task = asyncio.create_task(run_ovh_call(func, *args, timeout=timeout, **kwargs))
    started_at = time.monotonic()
    interval = max(0.001, float(heartbeat))
    while True:
        done, _ = await asyncio.wait({task}, timeout=interval)
        if done:
            return await task
        if on_wait is not None:
            elapsed = max(1, int(time.monotonic() - started_at))
            try:
                await on_wait(elapsed)
            except Exception as exc:
                logger.warning(f"更新 OVH 请求等待进度失败: {exc}")


async def execute_callback_safely(handler, update, context):
    """保证按钮处理异常时 Telegram 端一定得到可见反馈。"""
    try:
        return await handler(update, context)
    except Exception as exc:
        logger.error(f"按钮回调处理失败: {exc}\n{traceback.format_exc()}")
        query = getattr(update, "callback_query", None)
        if query is None:
            return None
        try:
            await query.answer("按钮处理失败", show_alert=True)
        except Exception:
            pass
        error_text = (
            "❌ 按钮操作失败\n\n"
            f"错误类型: {type(exc).__name__}\n"
            "请重新发送 /servers 后再试；详细原因已写入容器日志。"
        )
        try:
            await query.edit_message_text(error_text)
        except Exception:
            try:
                await query.message.reply_text(error_text)
            except Exception as reply_exc:
                logger.error(f"发送按钮错误提示失败: {reply_exc}")
        return None


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
        "MONITOR_INTERVAL":      ("monitor", "interval"),
        "MONITOR_AUTO_BUY":      ("monitor", "auto_buy"),
        "MONITOR_MAX_ORDERS":    ("monitor", "max_orders"),
        "MONITOR_ORDER_COOLDOWN":("monitor", "order_cooldown"),
        "MONITOR_DATACENTER":    ("monitor", "datacenter"),
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

    if "telegram" not in cfg:
        cfg["telegram"] = {}
    allow_all_env = os.environ.get("TG_ALLOW_ALL_USERS")
    if allow_all_env is not None:
        cfg["telegram"]["allow_all_users"] = _parse_bool(allow_all_env)
    else:
        cfg["telegram"]["allow_all_users"] = _parse_bool(
            cfg["telegram"].get("allow_all_users", False)
        )

    monitor_cfg = cfg.setdefault("monitor", {})
    monitor_cfg["auto_buy"] = _parse_bool(monitor_cfg.get("auto_buy", False))
    for key in ("interval", "max_orders", "order_cooldown"):
        if key in monitor_cfg:
            try:
                monitor_cfg[key] = int(monitor_cfg[key])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"monitor.{key} 必须是整数") from exc

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


def format_hardware_memory(memory_size) -> str:
    """格式化 OVH specifications/hardware.memorySize。"""
    if not isinstance(memory_size, dict):
        return ""
    value = memory_size.get("value")
    unit = str(memory_size.get("unit", "")).upper()
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if unit == "MB" and number >= 1024:
        gb = number / 1024
        return f"{int(gb) if gb.is_integer() else gb:g}GB"
    if unit == "KB" and number >= 1024 * 1024:
        gb = number / (1024 * 1024)
        return f"{int(gb) if gb.is_integer() else gb:g}GB"
    return f"{number:g}{unit}" if unit else f"{number:g}"


def classify_disk_group(disk_group: dict) -> tuple[str, str, str]:
    """返回 (类别, 显示名称, 图标)，明确区分 SSD 与 HDD。"""
    raw_type = str(disk_group.get("diskType", "") or "").strip()
    normalized = raw_type.lower().replace("-", "").replace("_", "")
    if "nvme" in normalized:
        return "ssd", "NVMe SSD", "⚡"
    if "ssd" in normalized or "solidstate" in normalized:
        return "ssd", "SSD", "⚡"
    if any(marker in normalized for marker in ("hdd", "sata", "sas", "rotational")):
        return "hdd", "HDD", "💽"
    return "unknown", raw_type or "未知磁盘", "💾"


def format_disk_group(disk_group: dict, default_group_id=None) -> str:
    group_id = disk_group.get("diskGroupId")
    disks = disk_group.get("numberOfDisks") or 0
    size = disk_group.get("diskSize", {}) or {}
    size_value = size.get("value", "?")
    size_unit = size.get("unit", "")
    _, type_label, icon = classify_disk_group(disk_group)
    default_text = " · 默认组" if str(group_id) == str(default_group_id) else ""
    return f"{icon} {type_label} · {disks}x{size_value}{size_unit} · group={group_id}{default_text}"


def select_default_raid_group(disk_groups: list, default_group_id=None):
    """选择单一 RAID0 磁盘组；绝不跨 SSD/HDD 或跨 group 组合。"""
    eligible = [
        group for group in disk_groups
        if group.get("diskGroupId") is not None and int(group.get("numberOfDisks") or 0) >= 2
    ]
    if not eligible:
        return None
    for group in eligible:
        if str(group.get("diskGroupId")) == str(default_group_id):
            return group
    return sorted(
        eligible,
        key=lambda group: (
            0 if classify_disk_group(group)[0] == "ssd" else 1,
            int(group.get("diskGroupId") or 0),
        ),
    )[0]


def select_default_system_group(disk_groups: list, default_group_id=None):
    """选择单个系统盘组；优先 OVH 默认组，其次 SSD，且绝不混合磁盘组。"""
    eligible = [
        group for group in disk_groups
        if group.get("diskGroupId") is not None and int(group.get("numberOfDisks") or 0) >= 1
    ]
    if not eligible:
        return None
    for group in eligible:
        if str(group.get("diskGroupId")) == str(default_group_id):
            return group
    return sorted(
        eligible,
        key=lambda group: (
            0 if classify_disk_group(group)[0] == "ssd" else 1,
            int(group.get("diskGroupId") or 0),
        ),
    )[0]


def select_default_ssh_key(keys: list, configured_key: str = ""):
    cleaned = [str(key).strip() for key in keys if str(key).strip()]
    if configured_key and configured_key in cleaned:
        return configured_key
    return cleaned[0] if cleaned else None


def server_creation_sort_key(server: dict) -> tuple:
    """优先使用新版服务 API 的秒级创建时间，再回退日期和原始顺序。"""
    raw_creation = str(
        server.get("exact_created_at") or server.get("created_at", "") or ""
    ).strip()
    creation_ts = 0.0
    if raw_creation:
        try:
            creation_ts = datetime.fromisoformat(
                raw_creation.replace("Z", "+00:00")
            ).timestamp()
        except (TypeError, ValueError):
            creation_ts = 0.0
    source_index = int(server.get("_source_index", -1) or -1)
    name_numbers = re.findall(r"\d+", str(server.get("name", "")))
    name_number = int(name_numbers[0]) if name_numbers else 0
    return creation_ts, source_index, name_number, str(server.get("name", ""))


def sort_servers_newest_first(servers: list) -> list:
    """最新发货服务器排第 1，原有服务器依次顺延。"""
    return sorted(servers, key=server_creation_sort_key, reverse=True)


def extract_installable_disk_groups(hardware: dict) -> list:
    """只保留 OVH 返回的有效物理磁盘组；无磁盘的暂停/删机服务会被过滤。"""
    if not isinstance(hardware, dict):
        return []
    groups = hardware.get("diskGroups")
    if not isinstance(groups, list):
        return []
    valid = []
    for group in groups:
        if not isinstance(group, dict) or group.get("diskGroupId") is None:
            continue
        try:
            disk_count = int(group.get("numberOfDisks") or 0)
        except (TypeError, ValueError):
            continue
        if disk_count > 0:
            valid.append(group)
    return valid


SERVER_LIST_PAGE_TEXT_LIMIT = 3400
SERVER_LIST_PAGE_SIZE = 4


def _truncate_server_entry(entry: dict, max_chars: int) -> dict:
    """按完整行截断单台服务器详情，避免破坏 Markdown 代码标记。"""
    text = str(entry.get("text", ""))
    if len(text) <= max_chars:
        return entry

    marker = "   … 其余详情已省略"
    lines = []
    used = 0
    for line in text.splitlines():
        added = len(line) + (1 if lines else 0)
        if used + added + len(marker) + 1 > max_chars:
            break
        lines.append(line)
        used += added
    if not lines:
        lines = ["⚠️ 单台服务器详情过长，已省略"]
    lines.append(marker)
    trimmed = dict(entry)
    trimmed["text"] = "\n".join(lines)
    return trimmed


def paginate_server_entries(
    entries: list,
    max_chars: int = SERVER_LIST_PAGE_TEXT_LIMIT,
    max_items: int = SERVER_LIST_PAGE_SIZE,
) -> list:
    """按正文长度和服务器数量分页，确保 Telegram 消息不会超限。"""
    if max_chars < 100 or max_items < 1:
        raise ValueError("分页限制无效")

    pages = []
    current = []
    current_chars = 0
    for raw_entry in entries:
        entry = _truncate_server_entry(raw_entry, max_chars)
        entry_text = str(entry.get("text", ""))
        separator_len = 2 if current else 0
        if current and (
            len(current) >= max_items
            or current_chars + separator_len + len(entry_text) > max_chars
        ):
            pages.append(current)
            current = []
            current_chars = 0
            separator_len = 0
        current.append(entry)
        current_chars += separator_len + len(entry_text)

    if current:
        pages.append(current)
    return pages or [[]]


def server_note_callback_data(operation: str, service_name: str, source: str) -> str:
    """生成不依赖内存操作表的备注按钮数据。"""
    op_code = {"miss": "m", "clear": "c"}.get(operation)
    source_code = {"finish": "f", "list": "l"}.get(source)
    if not op_code or not source_code:
        raise ValueError("无效的服务器备注操作")
    service_name = str(service_name or "").strip()
    if not service_name or "|" in service_name:
        raise ValueError("无效的 OVH 服务器名称")
    callback_data = f"sn|{source_code}{op_code}|{service_name}"
    if len(callback_data.encode("utf-8")) > 64:
        raise ValueError("服务器名称过长，无法生成 Telegram 备注按钮")
    return callback_data


def parse_server_note_callback(callback_data: str):
    parts = str(callback_data or "").split("|", 2)
    if len(parts) != 3 or parts[0] != "sn" or len(parts[1]) != 2:
        return None
    source = {"f": "finish", "l": "list"}.get(parts[1][0])
    operation = {"m": "miss", "c": "clear"}.get(parts[1][1])
    service_name = parts[2].strip()
    if not source or not operation or not service_name:
        return None
    return source, operation, service_name


def server_list_action_specs(
    index: int,
    action_id: str,
) -> list:
    """服务器总览只保留常用操作，备注管理放在选择后的页面。"""
    return [[
        {"text": f"🖥️ 选择 {index}", "callback_data": f"srv|select|{action_id}"},
        {"text": f"🔄 重启 {index}", "callback_data": f"srv|reboot|{action_id}"},
    ]]


def selected_server_action_specs(
    action_id: str,
    quick_available: bool = True,
    has_miss_note: bool = False,
) -> list:
    """选中服务器后提供安装与备注管理，备注按钮不出现在总览页。"""
    rows = []
    if quick_available:
        rows.append([{
            "text": "⚡ 一键 Debian 12 + 默认密钥 + RAID0",
            "callback_data": f"srv|quick|{action_id}",
        }])
    rows.append([{
        "text": "⚡ 一键 Debian 12 + 默认密钥 + 不组 RAID0",
        "callback_data": f"srv|quick_noraid|{action_id}",
    }])
    rows.append([{
        "text": "💿 手动选择系统",
        "callback_data": f"srv|install|{action_id}",
    }])
    rows.append([{
        "text": "🛟 救援模式启动",
        "callback_data": f"srv|rescue|{action_id}",
    }])
    rows.append([{
        "text": "📝 清除“没中”备注" if has_miss_note else "📝 标记“没中”",
        "callback_data": f"srvnote|{'clear' if has_miss_note else 'miss'}|{action_id}",
    }])
    return rows


REINSTALL_TEMPLATE_LABELS = {
    "debian12_64": "Debian 12",
    "debian13_64": "Debian 13",
    "ubuntu2404-server_64": "Ubuntu 24.04 LTS",
    "ubuntu2204-server_64": "Ubuntu 22.04 LTS",
    "proxmox8_64": "Proxmox VE 8",
    "proxmox9_64": "Proxmox VE 9",
    "rocky9_64": "Rocky Linux 9",
    "alma9_64": "AlmaLinux 9",
}


def reinstall_template_choices(default_template: str = "debian12_64") -> list:
    """返回无需查询 compatibleTemplates 即可显示的常用安装系统。"""
    ordered = list(REINSTALL_TEMPLATE_LABELS)
    if default_template in ordered:
        ordered.remove(default_template)
        ordered.insert(0, default_template)
    return [
        {
            "template": template,
            "label": REINSTALL_TEMPLATE_LABELS[template]
            + (" (默认)" if template == default_template else ""),
        }
        for template in ordered
    ]


def progress_bar_text(percent: int, width: int = 12) -> str:
    """返回固定宽度进度条。"""
    percent = max(0, min(100, int(percent)))
    filled = round(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def format_quick_install_progress(
    service_name: str,
    ip_address: str,
    percent: int,
    stage: str,
    detail: str = "",
) -> str:
    """生成一键安装准备阶段的可复制、可持续刷新的进度消息。"""
    lines = [
        "⚡ *一键安装进度*",
        "",
        f"🖥️ 服务器: `{service_name}`",
    ]
    if ip_address:
        lines.append(f"🌐 IP: `{ip_address}`")
    lines.extend([
        "",
        f"`{progress_bar_text(percent)}` {max(0, min(100, int(percent)))}%",
        f"📌 当前步骤: `{stage}`",
    ])
    if detail:
        lines.append(f"⏱️ {detail}")
    return "\n".join(lines)


def parse_running_reinstall_task(error) -> tuple[str, str] | None:
    """解析 OVH“已有重装任务”错误，返回 (task_id, status)。"""
    text = str(error or "")
    match = re.search(
        r"Task\s+(\d+)\s+of\s+type\s+reinstallServer\s+with\s+status\s+([a-zA-Z_]+)\s+is\s+already\s+running",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return match.group(1), match.group(2).lower()


INSTALL_TASK_SUCCESS_STATES = {"done", "finished", "completed", "success"}
INSTALL_TASK_FAILURE_STATES = {"error", "failed", "cancelled", "canceled"}


def reconcile_submitted_install_progress(
    status_text: str,
    percent: int,
    done: bool,
    task_status: str,
    current_os: str,
    activity_seen: bool,
) -> tuple[str, int, bool, bool]:
    """必须观察到本次安装先运行再结束，才允许显示完成。"""
    normalized_task = str(task_status or "").strip().lower()
    status_text = str(status_text or "等待 OVH 安装状态")
    percent = max(0, min(100, int(percent)))

    if normalized_task in INSTALL_TASK_FAILURE_STATES:
        return f"安装任务失败: {normalized_task}", 100, True, True

    install_failed = any(
        marker in status_text.lower() for marker in ("fail", "error", "失败")
    )
    if done and install_failed:
        return status_text, 100, True, True

    if not done:
        if normalized_task in INSTALL_TASK_SUCCESS_STATES:
            status_text = f"{status_text}（重装请求已受理）"
        elif normalized_task:
            status_text = f"{status_text}（OVH 任务: {normalized_task}）"
        return status_text, min(percent, 95), False, True
    if not activity_seen:
        task_hint = (
            "，重装请求已受理"
            if normalized_task in INSTALL_TASK_SUCCESS_STATES
            else f"，OVH 任务: {normalized_task}"
            if normalized_task
            else ""
        )
        return f"等待本次安装流程开始{task_hint}", 5, False, False
    return (
        f"安装流程完成，当前系统: {current_os or '待刷新'}",
        100,
        True,
        True,
    )


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


def watch_auto_buy_enabled(task: dict) -> bool:
    """旧版任务没有 auto_buy 字段，继续保持自动下单行为。"""
    return bool(task.get("auto_buy", True))


def watch_mode_label(task: dict) -> str:
    return "🚀 自动下单" if watch_auto_buy_enabled(task) else "🔔 仅通知"


def normalize_watch_round_orders(requested: int) -> int:
    """限制重新设置的本轮下单数量为 1-100。"""
    return max(1, min(int(requested), 100))


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
            timeout=20,
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
                         memory: str, storage: str, include_tax: bool = True,
                         breakdown: bool = False):
        """获取配置价格；breakdown=True 返回未税月费和一次性安装费。"""
        cart_id = None
        monthly_value = 0.0
        currency = "EUR"

        def add_item_total(item: dict):
            nonlocal monthly_value, currency
            for entry in (item or {}).get("prices", []):
                if entry.get("label") != "TOTAL":
                    continue
                price = entry.get("price", {})
                value = price.get("value")
                if isinstance(value, (int, float)):
                    monthly_value += float(value)
                currency = price.get("currencyCode", currency)

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
            add_item_total(item_result)

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
                                    option_item = self.post(
                                        f"/order/cart/{cart_id}/eco/options",
                                        itemId=item_id,
                                        planCode=wanted,
                                        duration=avail.get("duration", "P1M"),
                                        pricingMode=avail.get("pricingMode", "default"),
                                        quantity=1,
                                    )
                                    add_item_total(option_item)
                                except Exception:
                                    pass
                                break
                except Exception:
                    pass

            if breakdown:
                installation_value = 0.0
                catalog = self.get_catalog("eco")
                for plan in catalog.get("plans", []):
                    if plan.get("planCode") != plan_code:
                        continue
                    for pricing in plan.get("pricings", []):
                        if (
                            pricing.get("mode") == "default"
                            and pricing.get("capacities") == ["installation"]
                            and pricing.get("phase") == 0
                        ):
                            raw = pricing.get("price", 0)
                            installation_value = float(raw) / 100000000 if raw else 0.0
                            formatted = pricing.get("formattedPrice", "")
                            if formatted.startswith("$"):
                                currency = "USD"
                            elif "€" in formatted:
                                currency = "EUR"
                            break
                    break
                return {
                    "monthly": monthly_value,
                    "installation": installation_value,
                    "currency": currency,
                }

            # 兼容原调用：返回购物车首期总价。
            summary = self.get(f"/order/cart/{cart_id}/summary")
            prices = summary.get("prices", {})
            price_key = "withTax" if include_tax else "withoutTax"
            price_data = prices.get(price_key, {})
            price_value = price_data.get("value") if isinstance(price_data, dict) else price_data
            currency = price_data.get("currencyCode", "EUR") if isinstance(price_data, dict) else "EUR"

            if price_value is not None:
                if isinstance(price_value, (int, float)):
                    if price_value > 100000:
                        price_value = price_value / 100000000
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
            for source_index, name in enumerate(names):
                try:
                    info = self.get(f"/dedicated/server/{name}")
                    try:
                        service_info = self.get(f"/dedicated/server/{name}/serviceInfos")
                    except Exception:
                        service_info = {}
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
                        "created_at": service_info.get("creation", "") if isinstance(service_info, dict) else "",
                        "service_id": service_info.get("serviceId") if isinstance(service_info, dict) else None,
                        "_source_index": source_index,
                    })
                except Exception:
                    result.append({"name": name, "commercial_range": "?", "os": "?", "state": "?", "created_at": "", "_source_index": source_index})
            if result:
                newest_date = max(
                    (str(server.get("created_at", "") or "") for server in result),
                    default="",
                )
                for server in result:
                    if not newest_date or server.get("created_at") != newest_date:
                        continue
                    service_id = server.get("service_id")
                    if not service_id:
                        continue
                    try:
                        service = self.get(f"/services/{service_id}")
                        server["exact_created_at"] = (
                            service.get("billing", {})
                            .get("lifecycle", {})
                            .get("current", {})
                            .get("creationDate", "")
                        )
                    except Exception as exc:
                        logger.warning(f"读取 {server.get('name')} 精确创建时间失败: {exc}")
            return sort_servers_newest_first(result)
        except Exception:
            return []

    def get_server_info(self, service_name: str) -> dict:
        """获取单台服务器详情"""
        try:
            return self.get(f"/dedicated/server/{service_name}")
        except Exception:
            return {}

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

    def get_server_task(self, service_name: str, task_id: str) -> dict:
        """获取服务器任务状态"""
        try:
            return self.get(f"/dedicated/server/{service_name}/task/{task_id}")
        except Exception:
            return {}

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

    def create_ssh_key(self, key_name: str, public_key: str, key_type: str = "ed25519") -> dict:
        """将公钥添加到 OVH 账号；绝不上传私钥。"""
        return self.post(
            "/me/sshKey", keyName=key_name, key=public_key, type=key_type
        )

    def reinstall_server(self, service_name: str, template: str, hostname: str = None,
                         ssh_key_name: str = None, raid0: bool = False,
                         raid_disks: int = None, disk_group_id: int = None,
                         data_raid0: bool = False, data_disk_group_id: int = None,
                         data_raid_disks: int = None) -> dict:
        """重装系统 - 返回 task 信息"""
        body = {"operatingSystem": template}
        customizations = {}
        if hostname:
            customizations["hostname"] = hostname
        if ssh_key_name:
            customizations["sshKey"] = self.get_ssh_key_value(ssh_key_name)
        if customizations:
            body["customizations"] = customizations
        storage_config = []
        if raid0:
            if disk_group_id is None:
                raise ValueError("RAID0 必须指定单一 diskGroupId，禁止跨 SSD/HDD 组盘")
            if raid_disks is None or int(raid_disks) < 2:
                raise ValueError("RAID0 至少需要同一磁盘组内的 2 块磁盘")
            partitioning = {
                "layout": [
                    {"mountPoint": "/", "fileSystem": "ext4", "raidLevel": 0, "size": 0}
                ],
            }
            if raid_disks is not None:
                partitioning["disks"] = raid_disks
            storage_config.append({"diskGroupId": disk_group_id, "partitioning": partitioning})
        elif disk_group_id is not None:
            # 指定系统安装到某个磁盘组，但不做 RAID0；用于混合盘机器选择 NVMe 系统盘
            storage_config.append({"diskGroupId": disk_group_id})

        # 注意：OVH 当前不支持一次 reinstall 自定义多个 disk groups。
        # 如需 NVMe 系统盘 + HDD RAID0 数据盘，请先安装到 NVMe，再进系统手动组 /data。

        if storage_config:
            body["storage"] = storage_config
        return self.post(f"/dedicated/server/{service_name}/reinstall", **body)

    def reboot_server(self, service_name: str) -> dict:
        """硬重启服务器"""
        return self.post(f"/dedicated/server/{service_name}/reboot")

    def get_rescue_boot(self, service_name: str) -> dict:
        """获取服务器可用的 Rescue 启动项。"""
        for boot_id in self.get(f"/dedicated/server/{service_name}/boot"):
            boot = self.get(f"/dedicated/server/{service_name}/boot/{boot_id}")
            if boot.get("bootType") == "rescue":
                return boot
        return {}

    def set_rescue_boot(self, service_name: str, boot_id: int,
                        public_key: str = None, rescue_mail: str = None) -> dict:
        """设置下一次从 Rescue 启动，可使用 SSH 公钥或邮件密码。"""
        body = {
            "bootId": int(boot_id),
            "rescueSshKey": public_key,
            "rescueMail": rescue_mail,
        }
        return self.put(f"/dedicated/server/{service_name}", **body)

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
                  target_memory: str = None,
                  auto_pay: bool = False) -> dict:
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
            "auto_pay_requested": bool(auto_pay),
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
                order = self.checkout(cart_id, auto_pay=auto_pay)
                order_id = order.get("orderId")
                if not order_id:
                    raise RuntimeError("OVH 结账响应缺少 orderId，订单状态未知，请检查购物车")
                result["order_id"] = order_id
                result["payment_url"] = self.get_payment_url(order_id)

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


def friendly_plan_name(plan_code: str) -> str:
    """将 planCode 转为首选友好型号名，例如 24sk202 -> KS-2。"""
    normalized = str(plan_code or "").lower()
    for name, mapped_code in SERVER_NAME_MAP.items():
        if mapped_code.lower() == normalized and "-" in name:
            return name.upper()
    return str(plan_code or "")


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


def format_watchlist_task(plan_code: str, task: dict) -> str:
    """按带字段图标的多行结构格式化 /watchlist 任务。"""
    status = "🟢 监控中" if task.get("active") else "🔴 已停止"
    friendly = friendly_plan_name(plan_code)
    lines = [status, f"📦 型号: {friendly} ({plan_code})"]

    if task.get("dc"):
        lines.append(f"📍 机房: {format_dc(task['dc'])}")
    if task.get("excluded_dcs"):
        lines.append(
            "🚫 排除: " + ", ".join(format_dc(dc) for dc in task.get("excluded_dcs", []))
        )

    hardware_parts = []
    if task.get("storage"):
        hardware_parts.append(format_storage(task["storage"]))
    if task.get("memory"):
        hardware_parts.append(format_memory(task["memory"]))
    if hardware_parts:
        lines.append(f"💾 配置: {', '.join(hardware_parts)}")

    lines.append(f"⚙️ 模式: {watch_mode_label(task)}")
    lines.append(f"💳 自动付款: {'🟢 已开启' if task.get('auto_pay') else '🔴 已关闭'}")
    lines.append(f"📊 进度: {task.get('ordered', 0)}/{task.get('max_orders', 1)} 单")
    return "\n".join(lines)


def build_restock_snapshot(rows: list, allowed_plans: set = None) -> dict:
    """将 OVH 全量库存转换为配置+机房维度的有货状态快照。"""
    snapshot = {}
    for row in rows or []:
        plan_code = str(row.get("planCode") or row.get("fqn", "").split(".")[0])
        if not plan_code or (allowed_plans is not None and plan_code not in allowed_plans):
            continue
        fqn = str(row.get("fqn", ""))
        for dc_info in row.get("datacenters", []) or []:
            dc = str(dc_info.get("datacenter", ""))
            if not dc:
                continue
            status = str(dc_info.get("availability", "unknown"))
            key = f"{plan_code}|{fqn}|{dc}"
            snapshot[key] = {
                "available": status not in UNAVAILABLE_STATES,
                "plan_code": plan_code,
                "fqn": fqn,
                "memory": row.get("memory"),
                "storage": row.get("storage"),
                "dc": dc,
                "status": status,
            }
    return snapshot


def find_restock_events(previous: dict, current: dict) -> list:
    """仅返回明确从无货变为有货的库存项；首次快照不会通知。"""
    if not previous:
        return []
    return [
        item for key, item in current.items()
        if item.get("available") and key in previous and not previous[key].get("available")
    ]


def parse_server_available_email(subject: str) -> str | None:
    """从 OVH 发货邮件主题提取服务器名称。"""
    match = re.search(
        r"Your\s+(\S+\.ip-[\w.-]+)\s+dedicated server is available!",
        str(subject or ""),
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def parse_plan_code(text: str):
    """从文本中提取 planCode（兼容旧调用，内部使用 resolve_plan_code）"""
    text_lower = text.lower()
    # 1. 先查友好名称（按长度降序，避免 ks1 误匹配 ks1b）
    for name in sorted(SERVER_NAME_MAP.keys(), key=len, reverse=True):
        if name in text_lower:
            return SERVER_NAME_MAP[name]
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
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
    from telegram.ext import (
        ApplicationBuilder,
        CommandHandler,
        MessageHandler,
        CallbackQueryHandler,
        ContextTypes,
        filters,
    )

    tg_cfg = cfg.get("telegram", {})
    reinstall_defaults = cfg.get("defaults", {})
    bot_token = tg_cfg.get("bot_token", "")
    allowed_users = tg_cfg.get("allowed_users", [])
    allow_all_users = tg_cfg.get("allow_all_users", False)
    bot_app = None

    if not bot_token:
        logger.error("未配置 Telegram Bot Token")
        sys.exit(1)
    if not allowed_users and not allow_all_users:
        logger.error(
            "未配置 TG_ALLOWED_USERS。为防止陌生人操作下单，Bot 已拒绝启动；"
            "如确需公开访问，请显式设置 TG_ALLOW_ALL_USERS=true"
        )
        sys.exit(1)

    ovh_client = OVHClient(cfg)

    def check_user(user_id: int) -> bool:
        return is_user_allowed(user_id, allowed_users, allow_all_users)

    async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not check_user(update.effective_user.id):
            await update.message.reply_text("⛔ 未授权")
            return
        await update.message.reply_text(
            "🤖 *OVH 抢购 Bot 已就绪*\n\n"
            "常用入口:\n"
            "🛒 `/buy 型号` - 只显示当前有货配置，按钮抢购\n"
            "📡 `/watch 型号` - 显示全部配置，按钮设置监控\n"
            "📋 `/watchlist` - 查看和管理监控任务\n"
            "🔥 `/restock` - 全机型补货通知\n"
            "💳 `/status` - 查看最近订单\n"
            "🖥️ `/servers` - 服务器列表、重装、重启\n\n"
            "输入 `/help` 查看完整说明。\n"
            f"🌐 当前区域: `{ovh_client.zone}` / `{ovh_client.subsidiary}`",
            parse_mode="Markdown",
        )

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not check_user(update.effective_user.id):
            return
        await update.message.reply_text(
            "📖 *OVH Bot 帮助*\n\n"
            "🛒 *抢购*\n"
            "`/buy 型号`\n"
            "只列出当前有货的配置和机房，按按钮选择配置、机房、数量后下单。\n\n"
            "`/check 型号`\n"
            "查看该型号全部配置、全部机房的库存状态。\n\n"
            "📡 *监控*\n"
            "`/watch 型号`\n"
            "列出全部配置，包括当前无货配置；按按钮选择配置、机房、下单上限。\n\n"
            "`/watchlist`\n"
            "查看监控进度，并可暂停、启用、修改机房、改数量和删除任务。\n\n"
            "`/restock`\n"
            "管理 Eco 全机型补货通知；补货消息附带立即下单按钮。\n\n"
            "`/unwatch 型号`\n"
            "删除指定监控；不带型号时删除全部监控。\n\n"
            "💳 *订单*\n"
            "`/status`\n"
            "查看最近订单，支持翻页。\n\n"
            "`/status 订单号`\n"
            "查看订单详情、状态、价格和待付款链接。\n\n"
            "`/pay 订单号`\n"
            "获取指定订单付款链接。\n\n"
            "🖥️ *服务器*\n"
            "`/servers`\n"
            "查看服务器和可复制 IP；支持一键 Debian 12 + 默认密钥 + 单组 RAID0、"
            "手动重装、重启和“没中”备注。\n\n"
            "`/keys`\n"
            "查看 OVH 账户里的预设 SSH 密钥。\n\n"
            "📦 *目录*\n"
            "`/catalog`\n"
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
                "用法: `/buy <planCode>`\n\n"
                "示例: `/buy ks-1-b`\n\n"
                "然后用按钮选择配置和机房。",
                parse_mode="Markdown",
            )
            return

        plan_code = resolve_plan_code(context.args[0])
        if not plan_code:
            await update.message.reply_text(f"❌ 无法识别型号: {context.args[0]}\n\n可用名称: ks-1-b, ks-stor, ks-2, rise-2 等")
            return

        msg = await update.message.reply_text(f"🔍 正在查询 `{plan_code}` 可抢配置...", parse_mode="Markdown")
        all_configs = await asyncio.to_thread(ovh_client.check_availability, plan_code)
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
                f"💡 请用 `/watch` 先设定监控，等有货后自动下单。",
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
            await update.message.reply_text(
                "用法: `/check <planCode>`\n示例: `/check ks-1-b`",
                parse_mode="Markdown",
            )
            return

        plan_code = resolve_plan_code(context.args[0])
        if not plan_code:
            await update.message.reply_text(f"❌ 无法识别型号: {context.args[0]}\n\n可用名称: ks-1-b, ks-stor, ks-2, rise-2 等")
            return
        msg = await update.message.reply_text(f"🔍 正在查询 `{plan_code}` 所有配置的可用性...", parse_mode="Markdown")

        all_configs = await asyncio.to_thread(ovh_client.check_availability, plan_code)
        if not all_configs:
            await msg.edit_text(f"❌ 未获取到 `{plan_code}` 的可用性数据", parse_mode="Markdown")
            return

        # 获取基础价格（从 catalog）
        base_price_str = ""
        try:
            catalog = await asyncio.to_thread(ovh_client.get_catalog, 'eco')
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
                    price = await asyncio.to_thread(
                        ovh_client.get_config_price, plan_code, dc, cfg["memory"], cfg["storage"]
                    )
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
    #                         "auto_buy": bool, "max_orders": int, "ordered": int, "active": bool}}
    import os as _os
    DATA_DIR = _os.environ.get("OVH_BOT_DATA_DIR") or str(Path(__file__).parent / "data")
    WATCH_FILE = _os.path.join(DATA_DIR, "watch_tasks.json")
    RESTOCK_FILE = _os.path.join(DATA_DIR, "restock_monitor.json")
    DELIVERY_FILE = _os.path.join(DATA_DIR, "delivery_notifications.json")
    SERVER_NOTES_FILE = _os.path.join(DATA_DIR, "server_notes.json")
    SERVER_MARKS_DB = _os.path.join(DATA_DIR, "server_marks.db")

    def save_watch_tasks():
        """持久化监控任务到文件"""
        try:
            _os.makedirs(DATA_DIR, exist_ok=True)
            serializable = {}
            for pc, task in watch_tasks.items():
                serializable[pc] = {k: v for k, v in task.items() if not k.startswith("_")}
            temp_file = WATCH_FILE + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(serializable, f, ensure_ascii=False, indent=2)
                f.flush()
                _os.fsync(f.fileno())
            _os.replace(temp_file, WATCH_FILE)
        except Exception as e:
            logger.warning(f"保存监控任务失败: {e}")

    def load_watch_tasks():
        """从文件加载监控任务"""
        try:
            if _os.path.exists(WATCH_FILE):
                with open(WATCH_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("监控任务文件的顶层必须是对象")
                for task_id, task in data.items():
                    task.setdefault("auto_buy", True)
                    task.setdefault("plan_code", task_id)
                    task["_last_order_time"] = {}
                    watch_tasks[task_id] = task
                if watch_tasks:
                    logger.info(f"从文件恢复 {len(watch_tasks)} 个监控任务")
        except Exception as e:
            logger.warning(f"加载监控任务失败: {e}")

    restock_state = {"enabled": False, "chat_id": "", "snapshot": {}}

    def save_restock_state():
        try:
            _os.makedirs(DATA_DIR, exist_ok=True)
            temp_file = RESTOCK_FILE + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(restock_state, f, ensure_ascii=False)
                f.flush()
                _os.fsync(f.fileno())
            _os.replace(temp_file, RESTOCK_FILE)
        except Exception as exc:
            logger.warning(f"保存全机型补货状态失败: {exc}")

    def load_restock_state():
        try:
            if not _os.path.exists(RESTOCK_FILE):
                return
            with open(RESTOCK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                restock_state.update(data)
                restock_state.setdefault("snapshot", {})
        except Exception as exc:
            logger.warning(f"加载全机型补货状态失败: {exc}")

    load_restock_state()

    delivery_state = {"enabled": True, "chat_id": "", "seen_ids": []}

    def save_delivery_state():
        try:
            _os.makedirs(DATA_DIR, exist_ok=True)
            temp_file = DELIVERY_FILE + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(delivery_state, f, ensure_ascii=False)
                f.flush()
                _os.fsync(f.fileno())
            _os.replace(temp_file, DELIVERY_FILE)
        except Exception as exc:
            logger.warning(f"保存发货通知状态失败: {exc}")

    def load_delivery_state():
        try:
            if not _os.path.exists(DELIVERY_FILE):
                return
            with open(DELIVERY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                delivery_state.update(data)
                delivery_state["seen_ids"] = list(delivery_state.get("seen_ids", []))[-2000:]
        except Exception as exc:
            logger.warning(f"加载发货通知状态失败: {exc}")

    load_delivery_state()

    def init_server_marks_db():
        _os.makedirs(DATA_DIR, exist_ok=True)
        with sqlite3.connect(SERVER_MARKS_DB) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS server_marks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    service_name TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    note TEXT NOT NULL,
                    marked_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    cleared_at TEXT
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_server_marks_ip ON server_marks(ip)")

    def record_server_mark(service_name: str, ip: str, note: str = "没中"):
        if not ip:
            logger.warning(f"服务器 {service_name} 没有 IP，无法写入标记历史")
            return
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(SERVER_MARKS_DB) as conn:
            existing = conn.execute(
                "SELECT 1 FROM server_marks WHERE service_name=? AND ip=? AND note=? AND active=1",
                (service_name, ip, note),
            ).fetchone()
            if existing:
                return
            conn.execute(
                "UPDATE server_marks SET active=0, cleared_at=? WHERE service_name=? AND active=1",
                (now, service_name),
            )
            conn.execute(
                "INSERT INTO server_marks(service_name, ip, note, marked_at, active) VALUES(?,?,?,?,1)",
                (service_name, ip, note, now),
            )

    def clear_server_mark(service_name: str):
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(SERVER_MARKS_DB) as conn:
            conn.execute(
                "UPDATE server_marks SET active=0, cleared_at=? WHERE service_name=? AND active=1",
                (now, service_name),
            )

    def find_server_marks_by_ip(ip: str) -> list:
        with sqlite3.connect(SERVER_MARKS_DB) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT service_name, ip, note, marked_at, active, cleared_at "
                "FROM server_marks WHERE ip=? ORDER BY marked_at DESC",
                (ip,),
            ).fetchall()
        return [dict(row) for row in rows]

    init_server_marks_db()

    def save_server_notes():
        """原子保存服务器备注。"""
        try:
            _os.makedirs(DATA_DIR, exist_ok=True)
            temp_file = SERVER_NOTES_FILE + ".tmp"
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(server_notes, f, ensure_ascii=False, indent=2)
                f.flush()
                _os.fsync(f.fileno())
            _os.replace(temp_file, SERVER_NOTES_FILE)
        except Exception as e:
            logger.warning(f"保存服务器备注失败: {e}")

    def load_server_notes():
        try:
            if not _os.path.exists(SERVER_NOTES_FILE):
                return
            with open(SERVER_NOTES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("服务器备注文件的顶层必须是对象")
            server_notes.update(data)
        except Exception as e:
            logger.warning(f"加载服务器备注失败: {e}")

    def get_server_note(service_name: str) -> str:
        value = server_notes.get(service_name)
        if isinstance(value, dict):
            return str(value.get("note", "") or "")
        return str(value or "")

    watch_tasks = {}
    load_watch_tasks()  # 启动时恢复
    server_notes = {}
    load_server_notes()
    watch_running = False
    restock_running = False
    pending_actions = {}
    watch_sessions = {}
    buy_sessions = {}
    restock_buy_sessions = {}
    server_list_sessions = {}
    order_lock = asyncio.Lock()

    async def watch_monitor_loop():
        """后台监控循环"""
        nonlocal watch_running
        while watch_running:
            try:
                for task_id, task in list(watch_tasks.items()):
                    plan_code = task.get("plan_code", task_id)
                    if not task["active"]:
                        continue
                    auto_buy = watch_auto_buy_enabled(task)
                    if auto_buy and task["ordered"] >= task["max_orders"]:
                        task["active"] = False
                        save_watch_tasks()
                        await _send_msg(f"🎯 `{plan_code}` 已达到下单上限 ({task['max_orders']}单)，监控自动停止", task.get("chat_id"))
                        continue

                    try:
                        available = await asyncio.to_thread(
                            ovh_client.find_available_configs,
                            plan_code,
                            target_dc=task.get("dc"),
                            target_storage=task.get("storage"),
                            target_memory=task.get("memory"),
                        )
                        excluded = set(task.get("excluded_dcs", []))
                        if excluded:
                            available = [x for x in available if x.get("datacenter") not in excluded]
                        if available:
                            chosen = available[0]
                            # 成功下单后短冷却，失败后长冷却，避免刷屏但多单能更快继续
                            cooldown_key = f"{plan_code}|{chosen['datacenter']}|{chosen['fqn']}"
                            now = time.time()
                            last_order_time = task.get("_last_order_time", {})
                            if cooldown_key in last_order_time:
                                cd = last_order_time[cooldown_key]
                                if isinstance(cd, dict):
                                    last_ts = cd.get("ts", 0)
                                    cooldown_sec = cd.get("cooldown", 120)
                                else:
                                    last_ts = cd
                                    cooldown_sec = 120
                                if now - last_ts < cooldown_sec:
                                    continue

                            dc_display = format_dc(chosen['datacenter'])
                            if not auto_buy:
                                last_order_time[cooldown_key] = {"ts": now, "cooldown": 120}
                                task["_last_order_time"] = last_order_time
                                await _send_msg(
                                    f"🔥 *监控发现 `{plan_code}` 有货！*\n"
                                    f"📍 {dc_display} | {chosen['memory_display']} + {chosen['storage_display']}\n"
                                    f"🔔 当前为仅通知模式，不会自动下单。",
                                    task.get("chat_id")
                                )
                                continue

                            progress_message = await _send_msg(
                                f"🔥 *监控发现 `{plan_code}` 有货！*\n"
                                f"📍 {dc_display} | {chosen['memory_display']} + {chosen['storage_display']}\n"
                                f"🚀 正在自动下单... ({task['ordered']+1}/{task['max_orders']})",
                                task.get("chat_id")
                            )

                            server_type = guess_server_type(plan_code)
                            async with order_lock:
                                result = await asyncio.to_thread(
                                    ovh_client.quick_buy,
                                    plan_code=plan_code,
                                    server_type=server_type,
                                    datacenter=chosen["datacenter"],
                                    target_storage=chosen.get("storage") or task.get("storage"),
                                    target_memory=chosen.get("memory") or task.get("memory"),
                                    auto_pay=bool(task.get("auto_pay", False)),
                                )

                            if result["success"]:
                                task["ordered"] += 1
                                last_order_time[cooldown_key] = {"ts": now, "cooldown": 15}
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
                                if result.get("auto_pay_requested"):
                                    text += "💳 自动付款: 已使用 OVH 首选付款方式发起请求\n"
                                elif result["payment_url"]:
                                    text += f"💳 付款链接: {result['payment_url']}\n"
                                text += f"\n📊 监控进度: 已下 {task['ordered']}/{task['max_orders']} 单"
                                if task["ordered"] >= task["max_orders"]:
                                    task["active"] = False
                                    save_watch_tasks()
                                    text += "\n🎯 已达上限，监控自动停止\n\n⚠️ 请尽快手动付款以锁定订单！"
                                else:
                                    text += "\n\n⚠️ 请尽快手动付款以锁定订单！"
                            else:
                                # 失败加长冷却，避免库存瞬时变化时疯狂刷屏/重复请求
                                last_order_time[cooldown_key] = {"ts": now, "cooldown": 120}
                                task["_last_order_time"] = last_order_time
                                save_watch_tasks()
                                text = f"❌ 监控自动下单失败: `{plan_code}`\n{result['error']}"

                            await _edit_monitor_msg(
                                progress_message, text, task.get("chat_id")
                            )
                    except Exception as e:
                        logger.error(f"监控 {plan_code} 出错: {e}")

            except Exception as e:
                logger.error(f"监控循环出错: {e}")

            await asyncio.sleep(10)  # 每 10 秒检查一次

    async def delivery_notification_loop():
        """轮询 OVH 邮件历史，发现新主机发货后打开安装入口。"""
        while delivery_state.get("enabled"):
            try:
                ids = await asyncio.to_thread(ovh_client.get, "/me/notification/email/history")
                ids = [int(value) for value in ids if str(value).isdigit()]
                seen = {int(value) for value in delivery_state.get("seen_ids", []) if str(value).isdigit()}
                if not seen:
                    delivery_state["seen_ids"] = ids[-2000:]
                    save_delivery_state()
                else:
                    new_ids = sorted(set(ids) - seen)
                    for notification_id in new_ids:
                        item = await asyncio.to_thread(
                            ovh_client.get, f"/me/notification/email/history/{notification_id}"
                        )
                        service_name = parse_server_available_email(item.get("subject", ""))
                        if not service_name:
                            continue
                        try:
                            info = await asyncio.to_thread(ovh_client.get_server_info, service_name)
                            hardware = await asyncio.to_thread(ovh_client.get_server_hardware, service_name)
                            disk_groups = extract_installable_disk_groups(hardware)
                            default_group = hardware.get("defaultDiskGroupId") if isinstance(hardware, dict) else None
                            action_id = f"delivery_{notification_id}"
                            pending_actions[action_id] = {
                                "type": "server", "service_name": service_name,
                                "index": 0, "ip": info.get("ip", ""),
                                "commercial_range": info.get("commercialRange", "?"),
                                "datacenter": info.get("datacenter", "?"),
                                "os": info.get("os", "?"),
                                "state": info.get("state", "?"),
                                "memory": format_hardware_memory(hardware.get("memorySize")),
                                "disk_groups": disk_groups, "default_group": default_group,
                            }
                            disk_lines = ""
                            if disk_groups:
                                disk_lines = "💾 硬盘:\n" + "\n".join(
                                    f"　{format_disk_group(group, default_group)}"
                                    for group in disk_groups
                                ) + "\n"
                            text = (
                                f"🆕 *新服务器已发货*\n\n"
                                f"🖥️ 服务器: `{service_name}`\n"
                                f"📦 型号: `{info.get('commercialRange', '?')}`\n"
                                f"📍 机房: `{info.get('datacenter', '?')}`\n"
                                + (f"🧠 内存: `{format_hardware_memory(hardware.get('memorySize'))}`\n" if format_hardware_memory(hardware.get("memorySize")) else "")
                                + (f"🌐 IP: `{info.get('ip')}`\n" if info.get("ip") else "")
                                + disk_lines
                                + "\n请选择后续操作："
                            )
                            await bot_app.bot.send_message(
                                chat_id=str(delivery_state.get("chat_id") or tg_cfg.get("chat_id", "")),
                                text=text, parse_mode="Markdown",
                                reply_markup=InlineKeyboardMarkup([
                                    [InlineKeyboardButton("🛠️ 安装系统", callback_data=f"delivery|install|{action_id}")],
                                    [InlineKeyboardButton("🛟 救援模式启动", callback_data=f"srv|rescue|{action_id}")],
                                    [InlineKeyboardButton("📋 查看服务器", callback_data=f"delivery|view|{action_id}")],
                                ]),
                            )
                        except Exception as exc:
                            logger.error(f"处理新发货服务器 {service_name} 失败: {exc}")
                    delivery_state["seen_ids"] = ids[-2000:]
                    save_delivery_state()
            except Exception as exc:
                logger.error(f"读取 OVH 发货邮件通知失败: {exc}")
            await asyncio.sleep(60)

    async def restock_monitor_loop():
        """单次请求扫描 Eco 全机型，仅通知无货→有货变化。"""
        nonlocal restock_running
        while restock_running and restock_state.get("enabled"):
            try:
                catalog = await asyncio.to_thread(ovh_client.get_catalog, "eco")
                allowed_plans = {
                    str(plan.get("planCode")) for plan in catalog.get("plans", [])
                    if plan.get("planCode")
                }
                rows = await asyncio.to_thread(
                    ovh_client.get, "/dedicated/server/datacenter/availabilities"
                )
                current = build_restock_snapshot(rows, allowed_plans)
                previous = restock_state.get("snapshot", {})
                events = find_restock_events(previous, current)
                for item in events:
                    plan_code = item["plan_code"]
                    friendly = friendly_plan_name(plan_code)
                    storage = format_storage(item.get("storage"))
                    memory = format_memory(item.get("memory"))
                    dc = item.get("dc", "")
                    text = (
                        f"🔥 *全机型补货通知*\n\n"
                        f"📦 型号: {friendly} (`{plan_code}`)\n"
                        f"💾 配置: {memory} + {storage}\n"
                        f"📍 机房: {format_dc(dc)}\n"
                        f"✅ 库存: {format_dc_status(item.get('status'))}"
                    )
                    buy_id = str(int(time.time() * 1000000))[-14:]
                    restock_buy_sessions[buy_id] = item
                    keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "🛒 立即下单",
                            callback_data=f"restockbuy|{buy_id}",
                        )
                    ]])
                    try:
                        await bot_app.bot.send_message(
                            chat_id=str(restock_state.get("chat_id") or tg_cfg.get("chat_id", "")),
                            text=text,
                            parse_mode="Markdown",
                            reply_markup=keyboard,
                        )
                    except Exception as exc:
                        logger.error(f"发送全机型补货通知失败: {exc}")
                restock_state["snapshot"] = current
                save_restock_state()
            except Exception as exc:
                logger.error(f"全机型补货扫描失败: {exc}")
            await asyncio.sleep(60)
        restock_running = False

    async def _send_msg(text: str, chat_id: str = None):
        """发送消息到指定 chat 并返回 Message；未指定则回退到默认 chat。"""
        try:
            target_chat_id = str(chat_id or tg_cfg.get("chat_id", ""))
            if not target_chat_id or bot_app is None:
                logger.error(f"发送监控消息失败: chat_id={target_chat_id}, bot_app={bot_app is not None}")
                return None
            try:
                return await bot_app.bot.send_message(
                    chat_id=target_chat_id, text=text, parse_mode="Markdown"
                )
            except Exception as markdown_error:
                logger.error(f"监控 Markdown 消息发送失败，改用纯文本: {markdown_error}")
                return await bot_app.bot.send_message(chat_id=target_chat_id, text=text)
        except Exception as e:
            logger.error(f"发送监控消息失败: {e}")
            return None

    async def _edit_monitor_msg(message, text: str, chat_id: str = None):
        """优先编辑监控中的原消息；编辑失败时才降级发送新消息。"""
        if message is not None:
            try:
                await message.edit_text(text=text, parse_mode="Markdown")
                return message
            except Exception as markdown_error:
                logger.warning(f"编辑监控 Markdown 消息失败，改用纯文本: {markdown_error}")
                try:
                    await message.edit_text(text=text)
                    return message
                except Exception as edit_error:
                    logger.error(f"编辑监控消息失败，将降级发送新消息: {edit_error}")
        return await _send_msg(text, chat_id)

    def _progress_bar(percent: int, width: int = 12) -> str:
        return progress_bar_text(percent, width)

    def _extract_install_progress(status_obj, elapsed_sec: int = 0):
        """从 OVH 安装状态中提取阶段和百分比。"""
        if isinstance(status_obj, dict):
            progress = status_obj.get("progress")
            if isinstance(progress, list) and progress:
                total = len(progress)
                done_count = 0
                current = None
                has_error = False
                for step in progress:
                    st = str(step.get("status", "")).lower() if isinstance(step, dict) else ""
                    comment = str(step.get("comment", "")) if isinstance(step, dict) else str(step)
                    err = str(step.get("error", "")) if isinstance(step, dict) else ""
                    if err:
                        has_error = True
                        current = f"失败: {err}"
                        break
                    if st in ("done", "finished", "success", "ok"):
                        done_count += 1
                    elif st in ("doing", "running", "inprogress", "in_progress") and current is None:
                        current = comment or st
                if has_error:
                    return current, 100, True
                if done_count >= total:
                    return "安装步骤已完成", 100, True
                stage_text = current or "等待下一步"
                lower_stage = stage_text.lower()
                if "initial" in lower_stage:
                    percent = 5
                elif "hardware reboot" in lower_stage or "reboot" in lower_stage:
                    percent = 9
                elif "partition" in lower_stage or "format" in lower_stage:
                    percent = 25
                elif "install" in lower_stage or "copy" in lower_stage:
                    percent = 45
                elif "config" in lower_stage or "post" in lower_stage:
                    percent = 75
                else:
                    percent = min(95, max(5, int(elapsed_sec / 18)))
                return stage_text, percent, False

            status_text = str(status_obj.get("status") or status_obj.get("state") or status_obj.get("step") or status_obj)
            for key in ("percentage", "percent"):
                val = status_obj.get(key)
                if isinstance(val, (int, float)):
                    return status_text, int(val), int(val) >= 100
        else:
            status_text = str(status_obj)

        lower = status_text.lower()
        if "not being installed" in lower or "not being reinstalled" in lower:
            return "安装已结束或 OVH 暂无安装状态", 100, True
        if "fail" in lower:
            return status_text, 100, True
        estimated = min(95, max(5, int(elapsed_sec / 18)))
        return status_text, estimated, False

    async def track_install_progress(message, service_name: str, template: str, task_id: str = "?",
                                     ssh_key_name: str = None, raid_text: str = None, order_id: str = None,
                                     ip_address: str = None):
        """后台轮询安装状态并编辑同一条消息显示进度条。"""
        start_ts = time.time()
        last_text = None
        last_percent = 0
        task_activity_seen = False
        for _ in range(120):  # 最多跟踪约 40 分钟
            try:
                elapsed = int(time.time() - start_ts)
                status_obj = await asyncio.to_thread(ovh_client.get_install_status, service_name)
                status_text, percent, done = _extract_install_progress(status_obj, elapsed)
                server_info = await asyncio.to_thread(ovh_client.get_server_info, service_name)
                current_os = str(server_info.get("os", "")) if isinstance(server_info, dict) else ""
                task_info = await asyncio.to_thread(
                    ovh_client.get_server_task, service_name, task_id
                ) if task_id and task_id != "?" else {}
                task_status = str(task_info.get("status", "") or task_info.get("state", "")).lower() if isinstance(task_info, dict) else ""
                status_text, percent, done, task_activity_seen = reconcile_submitted_install_progress(
                    status_text,
                    percent,
                    done,
                    task_status,
                    current_os,
                    task_activity_seen,
                )
                percent = max(last_percent, percent)
                last_percent = percent
                bar = _progress_bar(percent)
                mins, secs = divmod(elapsed, 60)
                current_ip = str(server_info.get("ip", "") or ip_address or "") if isinstance(server_info, dict) else str(ip_address or "")
                text = (
                    f"💿 *系统安装进度*\n\n"
                    + f"🖥️ 服务器: `{service_name}`\n"
                    + (f"🌐 IP: `{current_ip}`\n" if current_ip else "")
                    + f"💿 系统: `{template}`\n"
                    + (f"🔑 SSH密钥: `{ssh_key_name}`\n" if ssh_key_name else "")
                    + (f"🧩 磁盘: `{raid_text}`\n" if raid_text else "")
                    + f"📋 任务ID: `{task_id}`\n"
                    + (f"🧾 订单号: `{order_id}`\n" if order_id else "")
                    + "\n"
                    + f"`{bar}` {percent}%\n"
                    + f"📌 状态: `{status_text}`\n"
                    + f"⏱️ 耗时: {mins}分{secs}秒"
                )
                reply_markup = None
                if done:
                    install_failed = (
                        task_status in ("error", "failed", "cancelled", "canceled")
                        or any(marker in status_text.lower() for marker in ("fail", "error", "失败"))
                    )
                    if install_failed:
                        text += "\n\n❌ 安装失败，请检查 OVH 任务详情"
                    elif isinstance(server_info, dict) and server_info:
                        info_lines = []
                        if server_info.get("datacenter"):
                            info_lines.append(f"📍 机房: `{server_info.get('datacenter')}`")
                        if server_info.get("os"):
                            info_lines.append(f"💻 当前系统: `{server_info.get('os')}`")
                        if server_info.get("state"):
                            info_lines.append(f"📌 状态: `{server_info.get('state')}`")
                        if server_info.get("commercialRange"):
                            info_lines.append(f"📦 型号: `{server_info.get('commercialRange')}`")
                        if info_lines:
                            text += "\n\n✅ 安装完成\n" + "\n".join(info_lines)
                        else:
                            text += "\n\n✅ 安装完成"
                    else:
                        text += "\n\n✅ 安装完成"

                    if not install_failed and get_server_note(service_name) != "没中":
                        note_callback = server_note_callback_data(
                            "miss", service_name, "finish"
                        )
                        reply_markup = InlineKeyboardMarkup([[
                            InlineKeyboardButton(
                                "📝 标记“没中”", callback_data=note_callback
                            )
                        ]])
                else:
                    text += "\n\n⏳ Bot 会自动刷新此进度。"

                if text != last_text:
                    await message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
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
                "用法: `/watch <planCode>`\n\n"
                "示例: `/watch ks-1-b`\n\n"
                "然后用按钮选择配置和机房",
                parse_mode="Markdown",
            )
            return

        plan_code = resolve_plan_code(context.args[0])
        if not plan_code:
            await update.message.reply_text(f"❌ 无法识别型号: {context.args[0]}\n\n可用名称: ks-1-b, ks-stor, ks-2, rise-2 等")
            return

        msg = await update.message.reply_text(f"🔍 正在查询 `{plan_code}` 可监控配置...", parse_mode="Markdown")
        all_configs = await asyncio.to_thread(ovh_client.check_availability, plan_code)
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
            "excluded_dcs": [],
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

        task_id = context.args[0]
        matching_ids = [
            current_id for current_id, task in watch_tasks.items()
            if task.get("plan_code", current_id) == resolve_plan_code(context.args[0])
        ]
        if task_id in watch_tasks:
            matching_ids = [task_id]
        if matching_ids:
            for current_id in matching_ids:
                watch_tasks[current_id]["active"] = False
                del watch_tasks[current_id]
            save_watch_tasks()
            await update.message.reply_text(
                f"📭 已取消 {len(matching_ids)} 个匹配的监控任务"
            )
        else:
            await update.message.reply_text(f"⚠️ 未找到匹配的监控任务")

    async def watchlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """查看当前监控列表"""
        if not check_user(update.effective_user.id):
            return

        if not watch_tasks:
            await update.message.reply_text(
                "📭 当前没有监控任务\n\n用 `/watch <planCode>` 开始监控",
                parse_mode="Markdown",
            )
            return

        text = "📡 *当前监控列表*\n\n"
        for task_id, task in watch_tasks.items():
            text += format_watchlist_task(task.get("plan_code", task_id), task) + "\n\n"

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("⚙️ 管理监控", callback_data="watchlist|manage")],
            [InlineKeyboardButton("取消", callback_data="cancel")],
        ])
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)

    async def restock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """查看和管理全机型补货通知。"""
        if not check_user(update.effective_user.id):
            return
        enabled = bool(restock_state.get("enabled"))
        status = "🟢 已启用" if enabled else "🔴 已停用"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "⏹ 停用补货通知" if enabled else "▶️ 启用补货通知",
                callback_data="restock|off" if enabled else "restock|on",
            )],
            [InlineKeyboardButton("取消", callback_data="cancel")],
        ])
        await update.message.reply_text(
            f"🔥 *全机型补货通知*\n\n"
            f"状态: {status}\n"
            f"范围: OVH Eco 目录全部机型\n"
            f"间隔: 每 60 秒扫描\n"
            f"通知: 仅无货变为有货时发送\n"
            f"下单: 通知附带立即下单按钮",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    async def catalog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not check_user(update.effective_user.id):
            return

        category = context.args[0] if context.args else "eco"
        msg = await update.message.reply_text(f"📖 正在获取 {category} 服务器目录...")

        catalog = await asyncio.to_thread(ovh_client.get_catalog, category)
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
            await update.message.reply_text(
                "用法: `/pay <orderId>`", parse_mode="Markdown"
            )
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
                orders, total = await asyncio.to_thread(ovh_client.list_recent_orders, 0, 10)
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
            await update.message.reply_text(
                "❌ 订单号必须是数字\n用法: `/status 254452143`",
                parse_mode="Markdown",
            )
            return

        try:
            msg = await update.message.reply_text(f"⏳ 正在查询订单 `{order_id}`...", parse_mode="Markdown")

            detail = await asyncio.to_thread(ovh_client.get_order_details, order_id)
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

    def render_servers_page(session_id: str, requested_page: int):
        session = server_list_sessions.get(session_id)
        if not session:
            return None
        pages = session["pages"]
        page = max(0, min(int(requested_page), len(pages) - 1))
        entries = pages[page]

        lines = [
            f"🖥️ 独立服务器列表 ({session['total']} 台) · 第 {page + 1}/{len(pages)} 页\n"
        ]
        keyboard = []
        for entry in entries:
            lines.append(entry["text"])
            lines.append("")
            keyboard.extend(entry.get("keyboard", []))
        lines.append("💡 先选择服务器，再选择一键安装或手动安装")

        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(
                "◀️ 上一页", callback_data=f"servers|p|{session_id}|{page - 1}"
            ))
        if page + 1 < len(pages):
            navigation.append(InlineKeyboardButton(
                "下一页 ▶️", callback_data=f"servers|p|{session_id}|{page + 1}"
            ))
        if navigation:
            keyboard.append(navigation)
        keyboard.append([InlineKeyboardButton("取消", callback_data="cancel")])
        return "\n".join(lines), InlineKeyboardMarkup(keyboard)

    async def servers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """列出所有独立服务器"""
        if not check_user(update.effective_user.id):
            return

        msg = await update.message.reply_text("⏳ 正在获取服务器列表...")
        try:
            servers = await asyncio.to_thread(ovh_client.list_servers)
            if not servers:
                await msg.edit_text("📭 没有找到独立服务器")
                return

            server_items = []
            for s in servers:
                hw = await asyncio.to_thread(ovh_client.get_server_hardware, s["name"])
                disk_groups = extract_installable_disk_groups(hw)
                if not disk_groups:
                    logger.info("/servers 隐藏无有效磁盘组的服务: %s", s["name"])
                    continue
                default_group = hw.get("defaultDiskGroupId") if isinstance(hw, dict) else None
                server_items.append((s, disk_groups, default_group))

            if not server_items:
                await msg.edit_text("📭 没有找到带有效磁盘组的服务器\n\n已隐藏退款后被 OVH 暂停或等待删机的无磁盘服务。")
                return

            session_id = str(int(time.time() * 1000))[-10:]
            entries = []
            for i, (s, disk_groups, default_group) in enumerate(server_items):
                state_emoji = {"ok": "🟢", "error": "🔴"}.get(s.get("state", ""), "🟡")

                entry_lines = [f"{state_emoji} {i+1}. `{s['name']}`"]
                entry_lines.append(f"   📦 `{s.get('commercial_range','?')}`")
                entry_lines.append(f"   💻 `{s.get('os','?')}` | 📍 `{s.get('datacenter','?')}`")
                if s.get("ip"):
                    entry_lines.append(f"   🌐 `{s['ip']}`")
                note = get_server_note(s["name"])
                if note:
                    entry_lines.append(f"   📝 备注: *{note}*")
                if disk_groups:
                    entry_lines.append("   💾 安装盘组（SSD/HDD 独立）:")
                    ordered_groups = sorted(
                        disk_groups,
                        key=lambda group: (
                            {"ssd": 0, "hdd": 1, "unknown": 2}[classify_disk_group(group)[0]],
                            int(group.get("diskGroupId") or 0),
                        ),
                    )
                    for dg in ordered_groups:
                        entry_lines.append(f"      {format_disk_group(dg, default_group)}")
                else:
                    entry_lines.append("   💾 安装盘组: OVH 未返回磁盘信息")

                action_id = f"srv{i+1}_{session_id[-6:]}"
                pending_actions[action_id] = {
                    "type": "server", "service_name": s["name"], "index": i+1,
                    "ip": s.get("ip", ""),
                    "disk_groups": disk_groups, "default_group": default_group,
                }
                entry_keyboard = [
                    [InlineKeyboardButton(**button) for button in row]
                    for row in server_list_action_specs(
                        i + 1, action_id
                    )
                ]
                entries.append({"text": "\n".join(entry_lines), "keyboard": entry_keyboard})

            server_list_sessions[session_id] = {
                "pages": paginate_server_entries(entries),
                "total": len(server_items),
                "created_at": time.time(),
            }
            if len(server_list_sessions) > 50:
                oldest = sorted(
                    server_list_sessions,
                    key=lambda key: server_list_sessions[key].get("created_at", 0),
                )[:-50]
                for old_session_id in oldest:
                    server_list_sessions.pop(old_session_id, None)

            text, markup = render_servers_page(session_id, 0)
            await msg.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        except Exception as e:
            await msg.edit_text(f"❌ 获取失败: {e}")

    async def keys_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """列出并添加 OVH 预设 SSH 密钥。"""
        if not check_user(update.effective_user.id):
            return
        try:
            keys = await asyncio.to_thread(ovh_client.list_ssh_keys)
            text = "🔑 *OVH 预设 SSH 密钥*\n\n"
            text += "\n".join(f"• `{k}`" for k in keys) if keys else "📭 当前没有预设 SSH 密钥"
            text += "\n\n💡 只上传公钥，私钥不会上传或保存。"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ 添加 SSH 公钥", callback_data="sshkey|add")],
                [InlineKeyboardButton("取消", callback_data="cancel")],
            ])
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

        servers = await asyncio.to_thread(ovh_client.list_servers)
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
            templates = await asyncio.to_thread(ovh_client.get_server_templates, service_name)
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
            keys = await asyncio.to_thread(ovh_client.list_ssh_keys)
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
            "ip": server.get("ip", ""),
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
            + (f"🌐 IP: `{server.get('ip')}`\n" if server.get("ip") else "")
            +
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
            await update.message.reply_text(
                "用法: `/reboot <序号或名称>`\n先用 `/servers` 查看列表",
                parse_mode="Markdown",
            )
            return

        servers = await asyncio.to_thread(ovh_client.list_servers)
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

    async def get_watch_untaxed_price_line(session: dict, dc: str = None) -> str:
        """获取并缓存 /watch 所选配置的未税价格文本。"""
        cfg = session.get("selected_cfg")
        if not cfg:
            return ""
        target_dc = dc or session.get("selected_dc")
        if not target_dc:
            excluded = set(session.get("excluded_dcs", []))
            candidates = [
                name for name, status in cfg.get("datacenters", {}).items()
                if name not in excluded and status not in UNAVAILABLE_STATES
            ]
            if not candidates:
                candidates = [name for name in cfg.get("datacenters", {}) if name not in excluded]
            target_dc = candidates[0] if candidates else None
        if not target_dc:
            return ""
        cache = session.setdefault("untaxed_price_cache", {})
        if target_dc not in cache:
            cache[target_dc] = await asyncio.to_thread(
                ovh_client.get_config_price,
                session["plan_code"], target_dc, cfg.get("memory"), cfg.get("storage"), False, True,
            )
        prices = cache.get(target_dc)
        if not isinstance(prices, dict):
            return ""
        symbol = "€" if prices.get("currency") == "EUR" else "$" if prices.get("currency") == "USD" else f"{prices.get('currency', 'EUR')} "
        monthly = float(prices.get("monthly", 0) or 0)
        installation = float(prices.get("installation", 0) or 0)
        return (
            f"\n💰 价格: {symbol}{monthly:.2f}/月\n"
            f"🔧 安装费: {symbol}{installation:.2f} (一次性)"
        )

    async def show_watch_count_prompt(query, context, session_id: str, back_callback: str):
        """提示用户直接发送 /watch 的下单数量。"""
        session = watch_sessions.get(session_id)
        if not session or not session.get("selected_cfg"):
            await query.edit_message_text("❌ 监控会话已过期，请重新 /watch")
            return
        cfg = session["selected_cfg"]
        dc = session.get("selected_dc")
        dc_display = "全部机房" if dc is None else format_dc(dc)
        await query.edit_message_text(
            f"⏳ 正在查询所选配置价格...\n\n"
            f"📦 型号: `{session['plan_code']}`\n"
            f"💾 配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}",
            parse_mode="Markdown",
        )
        price_line = await get_watch_untaxed_price_line(session, dc)
        context.user_data["watch_count_create"] = {
            "session_id": session_id,
            "message": query.message,
        }
        await query.edit_message_text(
            f"🎯 设置下单数量\n\n"
            f"📦 型号: `{session['plan_code']}`\n"
            f"💾 配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}\n"
            f"📍 机房: {dc_display}"
            f"{price_line}\n\n"
            f"请直接发送要下单的数量，例如：`5`\n"
            f"可设置范围：1–100 单",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ 返回上一步", callback_data=back_callback),
                InlineKeyboardButton("取消", callback_data="cancel"),
            ]]),
        )

    async def _button_callback_impl(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理内联按钮回调 - 支持带存储类型的下单"""
        nonlocal watch_running, restock_running
        query = update.callback_query
        try:
            await query.answer()
        except Exception as exc:
            # 回调确认过期不应阻断实际按钮操作。
            logger.warning(f"确认 Telegram 按钮回调失败，继续处理操作: {exc}")

        if not check_user(query.from_user.id):
            await query.answer("⛔ 未授权", show_alert=True)
            return

        data = query.data
        parts = data.split("|")
        context.user_data.pop("watch_count_edit", None)
        context.user_data.pop("watch_count_create", None)
        if parts[0] == "cancel":
            context.user_data.pop("sshkey_add", None)
            context.user_data.pop("rescue_mail", None)

        if parts[0] == "sshkey" and len(parts) >= 2 and parts[1] == "add":
            context.user_data["sshkey_add"] = {"stage": "name", "message": query.message}
            await query.edit_message_text(
                "➕ *添加 OVH SSH 公钥*\n\n请发送密钥名称，例如：`home-mac`\n\n只发送名称，不要发送私钥。",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("取消", callback_data="cancel")]]),
            )
            return

        if parts[0] == "delivery" and len(parts) >= 3:
            op = parts[1]
            action_id = parts[2]
            action = pending_actions.get(action_id)
            if not action:
                await query.edit_message_text("❌ 发货通知操作已过期，请使用 `/servers`", parse_mode="Markdown")
                return
            service_name = action["service_name"]
            if op == "install":
                rows = [
                    [InlineKeyboardButton(**button) for button in row]
                    for row in selected_server_action_specs(
                        action_id,
                        bool(select_default_raid_group(
                            action.get("disk_groups", []), action.get("default_group")
                        )),
                        get_server_note(service_name) == "没中",
                    )
                    if not any("备注" in button.get("text", "") for button in row)
                ]
                rows.append([
                    InlineKeyboardButton("⬅️ 返回", callback_data=f"delivery|home|{action_id}"),
                    InlineKeyboardButton("取消", callback_data="cancel"),
                ])
                await query.edit_message_text(
                    f"🛠️ *安装系统*\n\n"
                    f"🖥️ 服务器: `{service_name}`\n"
                    + (f"🌐 IP: `{action.get('ip')}`\n" if action.get("ip") else "")
                    + "\n请选择安装方式：",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(rows),
                )
            elif op == "view":
                lines = [
                    "📋 *服务器详情*\n",
                    f"🖥️ 服务器: `{service_name}`",
                    f"📦 型号: `{action.get('commercial_range', '?')}`",
                    f"💻 系统: `{action.get('os', '?')}`",
                    f"📍 机房: `{action.get('datacenter', '?')}`",
                    *([f"🧠 内存: `{action.get('memory')}`"] if action.get("memory") else []),
                    f"🟢 状态: `{action.get('state', '?')}`",
                ]
                if action.get("ip"):
                    lines.append(f"🌐 IP: `{action['ip']}`")
                groups = action.get("disk_groups", [])
                if groups:
                    lines.append("💾 安装盘组：")
                    lines.extend(
                        f"　{format_disk_group(group, action.get('default_group'))}"
                        for group in groups
                    )
                await query.edit_message_text(
                    "\n".join(lines), parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ 返回", callback_data=f"delivery|home|{action_id}"),
                        InlineKeyboardButton("取消", callback_data="cancel"),
                    ]]),
                )
            elif op == "home":
                text = (
                    f"🆕 *新服务器已发货*\n\n"
                    f"🖥️ `{service_name}`\n"
                    f"📦 型号: `{action.get('commercial_range', '?')}`\n"
                    f"📍 机房: `{action.get('datacenter', '?')}`\n"
                    + (f"🧠 内存: `{action.get('memory')}`\n" if action.get("memory") else "")
                    + (f"🌐 IP: `{action.get('ip')}`\n" if action.get("ip") else "")
                )
                await query.edit_message_text(
                    text, parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🛠️ 安装系统", callback_data=f"delivery|install|{action_id}")],
                        [InlineKeyboardButton("🛟 救援模式启动", callback_data=f"srv|rescue|{action_id}")],
                        [InlineKeyboardButton("📋 查看服务器", callback_data=f"delivery|view|{action_id}")],
                    ]),
                )
            return

        if parts[0] == "restockbuy" and len(parts) >= 2:
            item = restock_buy_sessions.get(parts[1])
            if not item:
                await query.edit_message_text("❌ 补货下单按钮已过期，请重新等待补货通知")
                return
            plan_code = item["plan_code"]
            all_configs = await asyncio.to_thread(ovh_client.check_availability, plan_code)
            selected = next((cfg for cfg in all_configs if cfg.get("fqn") == item.get("fqn")), None)
            if not selected:
                await query.edit_message_text("❌ 该补货配置已不可用，请等待下一次通知")
                return
            session_id = str(int(time.time() * 1000))[-10:]
            buy_sessions[session_id] = {
                "plan_code": plan_code,
                "all_configs": all_configs,
                "display_configs": all_configs,
                "selected_cfg": selected,
                "selected_dc": item.get("dc"),
                "target_storage": selected.get("storage"),
                "target_memory": selected.get("memory"),
                "count": 1,
            }
            keyboard = [
                [InlineKeyboardButton("1 单", callback_data=f"buy|count|{session_id}|1"), InlineKeyboardButton("2 单", callback_data=f"buy|count|{session_id}|2")],
                [InlineKeyboardButton("3 单", callback_data=f"buy|count|{session_id}|3"), InlineKeyboardButton("5 单", callback_data=f"buy|count|{session_id}|5")],
                [InlineKeyboardButton("10 单", callback_data=f"buy|count|{session_id}|10")],
                [InlineKeyboardButton("取消", callback_data="cancel")],
            ]
            await query.edit_message_text(
                f"🎯 选择下单数量\n\n"
                f"📦 型号: {friendly_plan_name(plan_code)} (`{plan_code}`)\n"
                f"💾 配置: {format_memory(selected['memory'])} + {format_storage(selected['storage'])}\n"
                f"📍 机房: {format_dc(item.get('dc'))}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        if parts[0] == "restock" and len(parts) >= 2:
            enabled = parts[1] == "on"
            restock_state["enabled"] = enabled
            restock_state["chat_id"] = str(query.message.chat_id)
            if enabled:
                restock_state["snapshot"] = {}
            save_restock_state()
            if enabled and not restock_running:
                restock_running = True
                asyncio.ensure_future(restock_monitor_loop())
            await query.edit_message_text(
                "✅ 全机型补货通知已启用，正在建立库存基线；后续补货会附带下单按钮。"
                if enabled else "⏹ 全机型补货通知已停用。"
            )
            return

        if parts[0] == "buy" and len(parts) >= 3 and parts[1] == "preset":
            plan_code = resolve_plan_code(parts[2])
            if not plan_code:
                return
            dc = parts[3]
            target_storage = parts[4] if len(parts) > 4 else None
            session_id = str(int(time.time() * 1000))[-10:]
            all_configs = await asyncio.to_thread(ovh_client.check_availability, plan_code)
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
            orders, total = await asyncio.to_thread(ovh_client.list_recent_orders, offset, 10)

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

        elif parts[0] == "servers" and len(parts) >= 4 and parts[1] == "p":
            session_id = parts[2]
            try:
                page = int(parts[3])
            except ValueError:
                return
            rendered = render_servers_page(session_id, page)
            if not rendered:
                await query.edit_message_text("❌ 服务器列表已过期，请重新 /servers")
                return
            text, markup = rendered
            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=markup,
            )

        elif parts[0] == "sn":
            parsed_note = parse_server_note_callback(data)
            if not parsed_note:
                await query.message.reply_text("❌ 无效的备注按钮，请重新 /servers")
                return
            note_source, note_op, service_name = parsed_note
            if note_op == "miss":
                server_notes[service_name] = {
                    "note": "没中",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                save_server_notes()
                info = await asyncio.to_thread(ovh_client.get_server_info, service_name)
                await asyncio.to_thread(record_server_mark, service_name, info.get("ip", ""))
                result_text = f"📝 已为 `{service_name}` 标记：*没中*"
            else:
                server_notes.pop(service_name, None)
                save_server_notes()
                await asyncio.to_thread(clear_server_mark, service_name)
                result_text = f"✅ 已清除 `{service_name}` 的“没中”备注"
                next_label = "📝 标记“没中”"
                next_callback = server_note_callback_data(
                    "miss", service_name, note_source
                )

            if note_source == "finish":
                if note_op == "miss":
                    await query.edit_message_reply_markup(reply_markup=None)
                else:
                    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(next_label, callback_data=next_callback)
                    ]]))
            else:
                await query.message.reply_text(result_text, parse_mode="Markdown")

        elif parts[0] == "srvnote" and len(parts) >= 3:
            note_op = parts[1]
            action_id = parts[2]
            action = pending_actions.get(action_id)
            if not action or not action.get("service_name"):
                await query.message.reply_text("❌ 备注操作已过期，请重新 /servers")
                return
            service_name = action["service_name"]
            if note_op == "miss":
                server_notes[service_name] = {
                    "note": "没中",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                save_server_notes()
                ip_address = action.get("ip", "")
                if not ip_address:
                    info = await asyncio.to_thread(ovh_client.get_server_info, service_name)
                    ip_address = info.get("ip", "")
                await asyncio.to_thread(record_server_mark, service_name, ip_address)
                result_text = f"📝 已为 `{service_name}` 标记：*没中*"
                next_label = "📝 清除“没中”备注"
                next_callback = f"srvnote|clear|{action_id}"
            elif note_op == "clear":
                server_notes.pop(service_name, None)
                save_server_notes()
                await asyncio.to_thread(clear_server_mark, service_name)
                result_text = f"✅ 已清除 `{service_name}` 的“没中”备注"
                next_label = "📝 标记“没中”"
                next_callback = f"srvnote|miss|{action_id}"
            else:
                return

            if action.get("type") == "server":
                rows = [
                    [InlineKeyboardButton(**button) for button in row]
                    for row in selected_server_action_specs(
                        action_id,
                        bool(select_default_raid_group(
                            action.get("disk_groups", []), action.get("default_group")
                        )),
                        get_server_note(service_name) == "没中",
                    )
                ]
                rows.append([InlineKeyboardButton("取消", callback_data="cancel")])
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup(rows)
                )
            elif action.get("type") == "server_note":
                await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(next_label, callback_data=next_callback)
                ]]))
            else:
                await query.message.reply_text(result_text, parse_mode="Markdown")

        elif parts[0] == "srv" and len(parts) >= 3:
            op = parts[1]
            action_id = parts[2]
            action = pending_actions.get(action_id)
            if not action:
                await query.edit_message_text("❌ 操作已过期，请重新 /servers")
                return
            service_name = action["service_name"]

            if op == "select":
                keyboard = [
                    [InlineKeyboardButton(**button) for button in row]
                    for row in selected_server_action_specs(
                        action_id,
                        bool(select_default_raid_group(
                            action.get("disk_groups", []), action.get("default_group")
                        )),
                        get_server_note(service_name) == "没中",
                    )
                ]
                keyboard.append([InlineKeyboardButton("取消", callback_data="cancel")])
                await query.edit_message_text(
                    f"🖥️ *已选择服务器*\n\n服务器: `{service_name}`"
                    + (f"\nIP: `{action.get('ip')}`" if action.get("ip") else "")
                    + "\n\n请选择安装方式：",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )

            elif op in ("quick", "quick_noraid"):
                quick_raid0 = op == "quick"
                await query.edit_message_text(
                    format_quick_install_progress(
                        service_name,
                        action.get("ip", ""),
                        10,
                        "读取默认安装设置",
                        "步骤 1/4 · 本地配置，通常立即完成",
                    ),
                    parse_mode="Markdown",
                )
                default_template = reinstall_defaults.get("reinstall_os", "debian12_64")
                configured_key = reinstall_defaults.get("ssh_key", "")
                if configured_key:
                    default_key = configured_key
                else:
                    await query.edit_message_text(
                        format_quick_install_progress(
                            service_name,
                            action.get("ip", ""),
                            30,
                            "读取 OVH SSH 密钥",
                            "步骤 2/4 · 最长等待 20 秒",
                        ),
                        parse_mode="Markdown",
                    )

                    async def update_key_wait(elapsed: int):
                        await query.edit_message_text(
                            format_quick_install_progress(
                                service_name,
                                action.get("ip", ""),
                                min(45, 30 + elapsed),
                                "等待 OVH 返回 SSH 密钥",
                                f"步骤 2/4 · 已等待 {elapsed} 秒，20 秒后自动超时",
                            ),
                            parse_mode="Markdown",
                        )

                    try:
                        keys = await run_ovh_call_with_heartbeat(
                            ovh_client.list_ssh_keys,
                            timeout=20,
                            heartbeat=5,
                            on_wait=update_key_wait,
                        )
                    except TimeoutError as exc:
                        await query.edit_message_text(
                            f"❌ 一键安装准备超时\n\n服务器: `{service_name}`\n`{exc}`",
                            parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("🔄 重试一键安装", callback_data=f"srv|{op}|{action_id}")],
                                [InlineKeyboardButton("💿 手动安装", callback_data=f"srv|install|{action_id}"), InlineKeyboardButton("取消", callback_data="cancel")],
                            ]),
                        )
                        return
                    default_key = select_default_ssh_key(keys)
                if not default_key:
                    await query.edit_message_text(
                        f"❌ 一键安装不可用\n\n服务器: `{service_name}`\n"
                        "账号没有 OVH SSH 密钥。请先添加密钥，或使用手动安装选择不使用密钥。",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("💿 手动安装", callback_data=f"srv|install|{action_id}"),
                            InlineKeyboardButton("取消", callback_data="cancel"),
                        ]]),
                    )
                    return

                disk_mode = "RAID0" if quick_raid0 else "不组 RAID0"
                await query.edit_message_text(
                    format_quick_install_progress(
                        service_name,
                        action.get("ip", ""),
                        65,
                        f"检查{disk_mode}安装盘",
                        "步骤 3/4 · SSD 与 HDD 分组选择，绝不混组",
                    ),
                    parse_mode="Markdown",
                )
                selected_group = (
                    select_default_raid_group(
                        action.get("disk_groups", []), action.get("default_group")
                    )
                    if quick_raid0
                    else select_default_system_group(
                        action.get("disk_groups", []), action.get("default_group")
                    )
                )
                if not selected_group:
                    unavailable_reason = (
                        "没有包含至少 2 块同类型磁盘的独立磁盘组，无法安全创建 RAID0。"
                        if quick_raid0
                        else "OVH 没有返回可用的系统安装磁盘组。"
                    )
                    await query.edit_message_text(
                        f"❌ 一键安装不可用\n\n服务器: `{service_name}`\n"
                        + unavailable_reason,
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("💿 手动安装", callback_data=f"srv|install|{action_id}"),
                            InlineKeyboardButton("取消", callback_data="cancel"),
                        ]]),
                    )
                    return

                group_id = selected_group["diskGroupId"]
                raid_disks = int(selected_group.get("numberOfDisks") or 0) if quick_raid0 else None
                raid_text = (
                    f"{format_disk_group(selected_group, action.get('default_group'))} · {disk_mode}"
                )
                confirm_id = str(int(time.time() * 1000))[-10:]
                pending_actions[confirm_id] = {
                    "type": "reinstall",
                    "service_name": service_name,
                    "ip": action.get("ip", ""),
                    "template": default_template,
                    "hostname": None,
                    "ssh_key_name": default_key,
                    "raid0": quick_raid0,
                    "raid_disks": raid_disks,
                    "disk_group_id": group_id,
                    "data_raid0": False,
                    "data_disk_group_id": None,
                    "data_raid_disks": None,
                    "raid_text": raid_text,
                    "quick_install": True,
                }
                await query.edit_message_text(
                    f"⚡ *一键安装进度*\n\n"
                    + f"🖥️ 服务器: `{service_name}`\n"
                    + (f"🌐 IP: `{action.get('ip')}`\n" if action.get("ip") else "")
                    + f"💿 系统: `{default_template}`\n"
                    + f"🔑 SSH 密钥: `{default_key}`\n"
                    + f"🧩 安装盘: `{raid_text}`\n\n"
                    + f"`{progress_bar_text(100)}` 100%\n"
                    + f"📌 当前步骤: `预设准备完成，等待确认`\n\n"
                    + f"SSD 与 HDD 不会混组；本次只使用 group={group_id}，模式为{disk_mode}。\n"
                    + f"🚨 确认后该组所有数据将被清除！",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⚠️ 确认一键安装", callback_data=f"act|{confirm_id}")],
                        [InlineKeyboardButton("⬅️ 手动选择", callback_data=f"srv|install|{action_id}"), InlineKeyboardButton("取消", callback_data="cancel")],
                    ]),
                )

            elif op == "rescue":
                try:
                    rescue_boot = await asyncio.to_thread(ovh_client.get_rescue_boot, service_name)
                    if not rescue_boot:
                        await query.edit_message_text("❌ 该服务器没有可用的 Rescue 启动项")
                        return
                    action["rescue_boot_id"] = rescue_boot["bootId"]
                    keys = await asyncio.to_thread(ovh_client.list_ssh_keys)
                    keyboard = []
                    for key_name in keys:
                        keyboard.append([InlineKeyboardButton(
                            f"🔑 SSH 密钥: {key_name}",
                            callback_data=f"srv|rescuekey|{action_id}|{key_name}",
                        )])
                    keyboard.append([InlineKeyboardButton(
                        "✉️ 邮件接收 Rescue 密码",
                        callback_data=f"srv|rescuemail|{action_id}",
                    )])
                    keyboard.append([
                        InlineKeyboardButton("⬅️ 返回服务器", callback_data=f"srv|select|{action_id}"),
                        InlineKeyboardButton("取消", callback_data="cancel"),
                    ])
                    await query.edit_message_text(
                        f"🛟 *救援模式启动*\n\n"
                        f"🖥️ 服务器: `{service_name}`\n"
                        f"💿 Rescue: `{rescue_boot.get('description', 'Rescue')}`\n\n"
                        f"请选择认证方式：",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                    )
                except Exception as exc:
                    await query.edit_message_text(f"❌ 获取 Rescue 启动项失败: {exc}")

            elif op == "rescuekey" and len(parts) >= 4:
                key_name = parts[3]
                try:
                    public_key = await asyncio.to_thread(ovh_client.get_ssh_key_value, key_name)
                except Exception as exc:
                    await query.edit_message_text(f"❌ 读取 SSH 公钥失败: {exc}")
                    return
                confirm_id = str(int(time.time() * 1000))[-10:]
                pending_actions[confirm_id] = {
                    "type": "rescue_boot", "service_name": service_name,
                    "boot_id": action.get("rescue_boot_id"),
                    "public_key": public_key, "key_name": key_name,
                    "rescue_mail": None,
                }
                await query.edit_message_text(
                    f"⚠️ *确认从救援模式启动*\n\n"
                    f"🖥️ 服务器: `{service_name}`\n"
                    f"🔑 登录方式: SSH 密钥 `{key_name}`\n\n"
                    f"确认后会设置 Rescue 网络启动并立即重启服务器，当前业务将中断。",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("⚠️ 确认进入 Rescue", callback_data=f"act|{confirm_id}")],
                        [InlineKeyboardButton("⬅️ 返回", callback_data=f"srv|rescue|{action_id}"), InlineKeyboardButton("取消", callback_data="cancel")],
                    ]),
                )

            elif op == "rescuemail":
                context.user_data["rescue_mail"] = {
                    "action_id": action_id, "message": query.message,
                }
                await query.edit_message_text(
                    f"✉️ *邮件接收 Rescue 密码*\n\n"
                    f"🖥️ 服务器: `{service_name}`\n\n"
                    f"请发送接收 Rescue 密码的邮箱地址。",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ 返回", callback_data=f"srv|rescue|{action_id}"),
                        InlineKeyboardButton("取消", callback_data="cancel"),
                    ]]),
                )

            elif op == "install":
                default_template = reinstall_defaults.get("reinstall_os", "debian12_64")
                keyboard = []
                for choice in reinstall_template_choices(default_template):
                    keyboard.append([InlineKeyboardButton(
                        choice["label"],
                        callback_data=f"srv|os|{action_id}|{choice['template']}",
                    )])
                keyboard.append([
                    InlineKeyboardButton("⬅️ 返回服务器", callback_data=f"srv|select|{action_id}"),
                    InlineKeyboardButton("取消", callback_data="cancel"),
                ])
                await query.edit_message_text(
                    f"💿 *手动选择系统*\n\n服务器: `{service_name}`"
                    + (f"\nIP: `{action.get('ip')}`" if action.get("ip") else "")
                    + "\n\n请选择系统：",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            elif op == "os" and len(parts) >= 4:
                action["template"] = parts[3]
                await query.edit_message_text(
                    f"⏳ 正在获取 SSH 密钥...\n\n服务器: `{service_name}`\n系统: `{action['template']}`",
                    parse_mode="Markdown",
                )
                try:
                    keys = await run_ovh_call(ovh_client.list_ssh_keys)
                except TimeoutError as exc:
                    await query.edit_message_text(
                        f"⚠️ 获取 SSH 密钥超时\n\n服务器: `{service_name}`\n`{exc}`\n\n可重试，或不使用密钥继续。",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🔄 重试获取密钥", callback_data=f"srv|os|{action_id}|{action['template']}")],
                            [InlineKeyboardButton("不使用 SSH key 继续", callback_data=f"srv|key|{action_id}|none")],
                            [InlineKeyboardButton("⬅️ 返回系统", callback_data=f"srv|install|{action_id}"), InlineKeyboardButton("取消", callback_data="cancel")],
                        ]),
                    )
                    return
                configured_key = reinstall_defaults.get("ssh_key", "")
                default_key = select_default_ssh_key(keys, configured_key)
                ordered_keys = ([default_key] if default_key else []) + [key for key in keys if key != default_key]
                keyboard = []
                for k in ordered_keys[:8]:
                    suffix = " (默认)" if k == default_key else ""
                    keyboard.append([InlineKeyboardButton(f"🔑 {k}{suffix}", callback_data=f"srv|key|{action_id}|{k}")])
                keyboard.append([InlineKeyboardButton("不使用 SSH key", callback_data=f"srv|key|{action_id}|none")])
                keyboard.append([
                    InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"srv|install|{action_id}"),
                    InlineKeyboardButton("取消", callback_data="cancel")
                ])
                await query.edit_message_text(
                    f"🔑 *选择 SSH 密钥*\n\n服务器: `{service_name}`\n系统: `{action['template']}`\n"
                    f"默认使用: `{default_key or '无可用密钥'}`",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            elif op == "key" and len(parts) >= 4:
                action["ssh_key_name"] = None if parts[3] == "none" else parts[3]
                keyboard = []
                disk_groups = sorted(
                    action.get("disk_groups", []),
                    key=lambda group: (
                        {"ssd": 0, "hdd": 1, "unknown": 2}[classify_disk_group(group)[0]],
                        int(group.get("diskGroupId") or 0),
                    ),
                )
                default_raid_group = select_default_raid_group(
                    disk_groups, action.get("default_group")
                )
                for dg in disk_groups:
                    group_id = dg.get("diskGroupId")
                    disks = dg.get("numberOfDisks") or 0
                    if group_id is None:
                        continue
                    size = dg.get("diskSize", {})
                    size_txt = f"{size.get('value','?')}{size.get('unit','')}"
                    _, disk_label, disk_icon = classify_disk_group(dg)
                    if disks >= 2:
                        recommended = " · 默认" if dg is default_raid_group else ""
                        keyboard.append([InlineKeyboardButton(
                            f"{disk_icon} {disk_label} {disks}x{size_txt} · RAID0{recommended}",
                            callback_data=f"srv|raid|{action_id}|g{group_id}d{disks}",
                        )])
                    keyboard.append([InlineKeyboardButton(
                        f"↳ {disk_label} {disks}x{size_txt} · 不做 RAID0",
                        callback_data=f"srv|raid|{action_id}|sys{group_id}",
                    )])

                # OVH reinstall API 不支持一次安装同时自定义多个磁盘组。
                # 混合盘机器只能先选择 NVMe 系统盘，HDD 数据盘 RAID0 需系统安装完成后进 SSH 手动创建。
                if not keyboard:
                    keyboard = [[InlineKeyboardButton("默认分区 / 无 RAID", callback_data=f"srv|raid|{action_id}|none")]]
                keyboard.append([
                    InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"srv|os|{action_id}|{action['template']}"),
                    InlineKeyboardButton("取消", callback_data="cancel")
                ])
                inventory = "\n".join(
                    f"• {format_disk_group(group, action.get('default_group'))}"
                    for group in disk_groups
                ) or "• OVH 未返回磁盘组"
                await query.edit_message_text(
                    f"🧩 *选择系统安装盘*\n\n"
                    f"服务器: `{service_name}`\n系统: `{action['template']}`\n"
                    f"SSH key: `{action.get('ssh_key_name') or '不使用'}`\n\n"
                    f"*磁盘组清单*\n{inventory}\n\n"
                    f"每个选项只使用一个 group，SSD 与 HDD 不会混组。",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            elif op == "raid" and len(parts) >= 4:
                mode = parts[3]
                if mode.startswith("sysauto"):
                    try:
                        group_id = int(mode[7:])
                    except ValueError:
                        await query.edit_message_text("❌ 磁盘方案参数无效，请重新 /servers")
                        return
                    dg = next((x for x in action.get("disk_groups", []) if str(x.get("diskGroupId")) == str(group_id)), None)
                    if not dg:
                        await query.edit_message_text("❌ 磁盘组不存在，请重新 /servers")
                        return
                    disks = dg.get("numberOfDisks") if dg else "?"
                    size = dg.get("diskSize", {}) if dg else {}
                    size_txt = f"{size.get('value','?')}{size.get('unit','')}"
                    action["raid0"] = False
                    action["disk_group_id"] = group_id
                    action["raid_disks"] = None
                    action["data_raid0"] = False
                    action["data_disk_group_id"] = None
                    action["data_raid_disks"] = None
                    _, disk_label, _ = classify_disk_group(dg or {})
                    raid_text = f"{disk_label} group={group_id} {disks}x{size_txt} / 不做 RAID0"
                elif mode.startswith("sys"):
                    try:
                        group_id = int(mode[3:])
                    except ValueError:
                        await query.edit_message_text("❌ 磁盘方案参数无效，请重新 /servers")
                        return
                    dg = next((x for x in action.get("disk_groups", []) if str(x.get("diskGroupId")) == str(group_id)), None)
                    if not dg:
                        await query.edit_message_text("❌ 磁盘组不存在，请重新 /servers")
                        return
                    disks = dg.get("numberOfDisks") if dg else "?"
                    size = dg.get("diskSize", {}) if dg else {}
                    size_txt = f"{size.get('value','?')}{size.get('unit','')}"
                    action["raid0"] = False
                    action["disk_group_id"] = group_id
                    action["raid_disks"] = None
                    action["data_raid0"] = False
                    action["data_disk_group_id"] = None
                    action["data_raid_disks"] = None
                    _, disk_label, _ = classify_disk_group(dg or {})
                    raid_text = f"{disk_label} group={group_id} {disks}x{size_txt} / 不做 RAID0"
                elif mode.startswith("g") and "d" in mode:
                    try:
                        group_part, disk_part = mode[1:].split("d", 1)
                        group_id = int(group_part)
                        disks = int(disk_part)
                    except ValueError:
                        await query.edit_message_text("❌ 磁盘方案参数无效，请重新 /servers")
                        return
                    dg = next((x for x in action.get("disk_groups", []) if str(x.get("diskGroupId")) == str(group_id)), None)
                    if not dg:
                        await query.edit_message_text("❌ 磁盘组不存在，请重新 /servers")
                        return
                    actual_disks = int(dg.get("numberOfDisks") or 0)
                    if actual_disks < 2 or actual_disks != disks:
                        await query.edit_message_text("❌ RAID0 磁盘数量与 OVH 规格不一致，请重新 /servers")
                        return
                    size = dg.get("diskSize", {}) if dg else {}
                    size_txt = f"{size.get('value','?')}{size.get('unit','')}"
                    action["raid0"] = True
                    action["disk_group_id"] = group_id
                    action["raid_disks"] = disks
                    action["data_raid0"] = False
                    action["data_disk_group_id"] = None
                    action["data_raid_disks"] = None
                    _, disk_label, _ = classify_disk_group(dg)
                    raid_text = f"{disk_label} group={group_id} {disks}x{size_txt} / RAID0"
                else:
                    action["raid0"] = False
                    action["disk_group_id"] = None
                    action["raid_disks"] = None
                    action["data_raid0"] = False
                    action["data_disk_group_id"] = None
                    action["data_raid_disks"] = None
                    raid_text = "默认分区 / 无 RAID"

                confirm_id = str(int(time.time() * 1000))[-10:]
                pending_actions[confirm_id] = {
                    "type": "reinstall",
                    "service_name": service_name,
                    "ip": action.get("ip", ""),
                    "template": action["template"],
                    "hostname": None,
                    "ssh_key_name": action.get("ssh_key_name"),
                    "raid0": action.get("raid0", False),
                    "raid_disks": action.get("raid_disks"),
                    "disk_group_id": action.get("disk_group_id"),
                    "data_raid0": action.get("data_raid0", False),
                    "data_disk_group_id": action.get("data_disk_group_id"),
                    "data_raid_disks": action.get("data_raid_disks"),
                    "raid_text": raid_text,
                }
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚠️ 确认安装", callback_data=f"act|{confirm_id}")],
                    [InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"srv|key|{action_id}|{action.get('ssh_key_name') or 'none'}"), InlineKeyboardButton("取消", callback_data="cancel")],
                ])
                await query.edit_message_text(
                    f"⚠️ 确认安装系统\n\n"
                    + f"服务器: `{service_name}`\n"
                    + (f"IP: `{action.get('ip')}`\n" if action.get("ip") else "")
                    + f"系统: `{action['template']}`\n"
                    + f"SSH key: `{action.get('ssh_key_name') or '不使用'}`\n"
                    + f"磁盘: `{raid_text}`\n\n"
                    + f"🚨 所有数据将被清除！",
                    parse_mode="Markdown",
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
                excluded = set(session.get("excluded_dcs", []))
                dcs = list(cfg["datacenters"].items())
                keyboard = []
                keyboard.append([InlineKeyboardButton("🌐 全部机房", callback_data=f"watch|dc|{session_id}|all")])
                for dc, status in dcs:
                    status_cn = format_dc_status(status)
                    mark = "❌" if dc in excluded else "✅"
                    keyboard.append([InlineKeyboardButton(f"{mark} {format_dc(dc)} ({status_cn})", callback_data=f"watch|dc|{session_id}|{dc}")])
                keyboard.append([
                    InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"watch|cfgback|{session_id}"),
                    InlineKeyboardButton("下一步", callback_data=f"watch|exnext|{session_id}"),
                ])
                keyboard.append([InlineKeyboardButton("取消", callback_data="cancel")])
                await query.edit_message_text(
                    f"🚫 排除机房（可多选）\n\n型号: `{plan_code}`\n配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}\n\n点机房可切换排除/恢复，点下一步继续。",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            elif stage == "exnext":
                cfg = session.get("selected_cfg")
                if not cfg:
                    await query.edit_message_text("❌ 会话状态丢失，请重新 /watch")
                    return
                excluded = set(session.get("excluded_dcs", []))
                dcs = list(cfg["datacenters"].items())
                keyboard = []
                keyboard.append([InlineKeyboardButton("🌐 全部机房", callback_data=f"watch|dc|{session_id}|all")])
                for dc, status in dcs:
                    if dc in excluded:
                        continue
                    status_cn = format_dc_status(status)
                    keyboard.append([InlineKeyboardButton(f"{format_dc(dc)} ({status_cn})", callback_data=f"watch|scope|{session_id}|{dc}")])
                keyboard.append([
                    InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"watch|dcback|{session_id}"),
                    InlineKeyboardButton("取消", callback_data="cancel")
                ])
                await query.edit_message_text(
                    f"📍 选择监控范围\n\n型号: `{plan_code}`\n配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}\n已排除: {', '.join(format_dc(d) for d in excluded) if excluded else '无'}",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

            elif stage == "countback":
                cfg = session.get("selected_cfg")
                dc = session.get("selected_dc")
                if not cfg:
                    await query.edit_message_text("❌ 会话状态丢失，请重新 /watch")
                    return
                await show_watch_count_prompt(
                    query, context, session_id, f"watch|dcback|{session_id}"
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
                session["excluded_dcs"] = []
                keyboard = []
                keyboard.append([InlineKeyboardButton("🌐 全部机房", callback_data=f"watch|dc|{session_id}|all")])
                for dc, status in dcs:
                    status_cn = format_dc_status(status)
                    keyboard.append([InlineKeyboardButton(f"✅ {format_dc(dc)} ({status_cn})", callback_data=f"watch|dc|{session_id}|{dc}")])
                keyboard.append([
                    InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"watch|cfgback|{session_id}"),
                    InlineKeyboardButton("下一步", callback_data=f"watch|exnext|{session_id}"),
                ])
                keyboard.append([InlineKeyboardButton("取消", callback_data="cancel")])
                await query.edit_message_text(
                    f"⏳ 正在查询所选配置价格...\n\n"
                    f"📦 型号: `{plan_code}`\n"
                    f"💾 配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}",
                    parse_mode="Markdown",
                )
                price_line = await get_watch_untaxed_price_line(session)
                title = f"🚫 排除机房（可多选）\n\n型号: `{plan_code}`\n配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}{price_line}\n\n点机房可切换排除/恢复，点下一步继续。"
                if not dcs:
                    title = f"📍 这个配置没有可选机房\n\n型号: `{plan_code}`\n配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}{price_line}"
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
                    await show_watch_count_prompt(
                        query, context, session_id, f"watch|dcback|{session_id}"
                    )
                else:
                    excluded = set(session.get("excluded_dcs", []))
                    if dc in excluded:
                        excluded.remove(dc)
                    else:
                        excluded.add(dc)
                    session["excluded_dcs"] = sorted(excluded)
                    dcs = list(cfg["datacenters"].items())
                    keyboard = []
                    keyboard.append([InlineKeyboardButton("🌐 全部机房", callback_data=f"watch|dc|{session_id}|all")])
                    for dc2, status in dcs:
                        status_cn = format_dc_status(status)
                        mark = "❌" if dc2 in excluded else "✅"
                        keyboard.append([InlineKeyboardButton(f"{mark} {format_dc(dc2)} ({status_cn})", callback_data=f"watch|dc|{session_id}|{dc2}")])
                    keyboard.append([
                        InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"watch|cfgback|{session_id}"),
                        InlineKeyboardButton("下一步", callback_data=f"watch|exnext|{session_id}"),
                    ])
                    keyboard.append([InlineKeyboardButton("取消", callback_data="cancel")])
                    await query.edit_message_text(
                        f"🚫 排除机房（可多选）\n\n型号: `{plan_code}`\n配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}\n\n已排除: {', '.join(format_dc(d) for d in excluded) if excluded else '无'}",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )

            elif stage == "scope" and len(parts) >= 4:
                dc = parts[3]
                cfg = session.get("selected_cfg")
                if not cfg:
                    await query.edit_message_text("❌ 会话状态丢失，请重新 /watch")
                    return
                session["selected_dc"] = dc
                await show_watch_count_prompt(
                    query, context, session_id, f"watch|exnext|{session_id}"
                )

            elif stage == "count" and len(parts) >= 4:
                val = parts[3]
                session["max_orders"] = int(val)
                cfg = session.get("selected_cfg")
                dc = session.get("selected_dc")
                if not cfg:
                    await query.edit_message_text("❌ 会话状态丢失，请重新 /watch")
                    return
                dc_display = "全部机房" if dc is None else format_dc(dc)
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("🚀 自动下单（默认）", callback_data=f"watch|mode|{session_id}|auto")],
                    [InlineKeyboardButton("🔔 仅通知", callback_data=f"watch|mode|{session_id}|notify")],
                    [InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"watch|countback|{session_id}"), InlineKeyboardButton("取消", callback_data="cancel")],
                ])
                await query.edit_message_text(
                    f"⚙️ 选择监控模式\n\n"
                    f"📦 型号: `{plan_code}`\n"
                    f"💾 配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}\n"
                    f"📍 机房: {dc_display}\n"
                    f"🎯 下单上限: {session.get('max_orders', 1)}",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )

            elif stage == "mode" and len(parts) >= 4:
                cfg = session.get("selected_cfg")
                dc = session.get("selected_dc")
                if not cfg:
                    await query.edit_message_text("❌ 会话状态丢失，请重新 /watch")
                    return
                auto_buy = parts[3] != "notify"
                session["auto_buy"] = auto_buy
                dc_display = "全部机房" if dc is None else format_dc(dc)
                price_line = await get_watch_untaxed_price_line(session, dc)
                confirm_id = str(int(time.time() * 1000))[-10:]
                pending_actions[confirm_id] = {
                    "type": "watch_start",
                    "plan_code": plan_code,
                    "fqn": cfg["fqn"],
                    "dc": dc,
                    "excluded_dcs": session.get("excluded_dcs", []),
                    "storage": cfg.get("storage"),
                    "memory": cfg.get("memory"),
                    "max_orders": session.get("max_orders", 1),
                    "auto_buy": auto_buy,
                }
                keyboard = InlineKeyboardMarkup([
                    [InlineKeyboardButton("▶️ 确认开始监控", callback_data=f"act|{confirm_id}")],
                    [InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"watch|countback|{session_id}"), InlineKeyboardButton("取消", callback_data="cancel")],
                ])
                await query.edit_message_text(
                    f"📡 确认开始监控\n\n"
                    f"📦 型号: `{plan_code}`\n"
                    f"💾 配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}\n"
                    f"📍 机房: {dc_display}"
                    f"{price_line}\n"
                    f"⚙️ 模式: {watch_mode_label({'auto_buy': auto_buy})}\n"
                    f"🎯 下单上限: {session.get('max_orders', 1)}",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )

            elif stage == "countback":
                cfg = session.get("selected_cfg")
                if not cfg:
                    await query.edit_message_text("❌ 会话状态丢失，请重新 /watch")
                    return
                await show_watch_count_prompt(
                    query, context, session_id, f"watch|dcback|{session_id}"
                )

        elif parts[0] == "watchlist":
            if len(parts) >= 2 and parts[1] == "manage":
                if not watch_tasks:
                    await query.edit_message_text("📭 当前没有监控任务")
                    return
                keyboard = []
                for task_id, task in watch_tasks.items():
                    plan_code = task.get("plan_code", task_id)
                    status_icon = "🟢" if task.get("active") else "🔴"
                    keyboard.append([
                        InlineKeyboardButton(
                            f"{status_icon} {friendly_plan_name(plan_code)} · {format_storage(task.get('storage'))} ({task.get('ordered', 0)}/{task.get('max_orders', 1)})",
                            callback_data=f"watchlist|task|{task_id}"
                        )
                    ])
                keyboard.append([InlineKeyboardButton("取消", callback_data="cancel")])
                await query.edit_message_text(
                    "⚙️ 选择要管理的监控任务",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return

            if len(parts) >= 3 and parts[1] == "task":
                task_id = parts[2]
                task = watch_tasks.get(task_id)
                plan_code = task.get("plan_code", task_id) if task else task_id
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
                    callback_data=f"watchlist|toggle|{task_id}"
                )
                mode_btn = InlineKeyboardButton(
                    "🔔 改为仅通知" if watch_auto_buy_enabled(task) else "🚀 改为自动下单",
                    callback_data=f"watchlist|mode|{task_id}"
                )
                keyboard = InlineKeyboardMarkup([
                    [action_btn],
                    [mode_btn],
                    [InlineKeyboardButton("📍 修改监控机房", callback_data=f"watchlist|dcs|{task_id}")],
                    [InlineKeyboardButton(
                        "💳 关闭自动付款" if task.get("auto_pay") else "💳 开启自动付款",
                        callback_data=f"watchlist|autopay|{task_id}",
                    )],
                    [InlineKeyboardButton("🎯 重新设置下单数量", callback_data=f"watchlist|count|{task_id}")],
                    [InlineKeyboardButton("🗑 删除监控", callback_data=f"watchlist|delete|{task_id}")],
                    [InlineKeyboardButton("⬅️ 返回任务列表", callback_data="watchlist|manage"), InlineKeyboardButton("取消", callback_data="cancel")],
                ])
                await query.edit_message_text(
                    f"⚙️ 管理监控任务\n\n{format_watchlist_task(plan_code, task)}",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
                return

            if len(parts) >= 3 and parts[1] in {"autopay", "autopayconfirm"}:
                task_id = parts[2]
                task = watch_tasks.get(task_id)
                plan_code = task.get("plan_code", task_id) if task else task_id
                if not task:
                    await query.edit_message_text("❌ 监控任务不存在或已删除")
                    return
                if parts[1] == "autopay" and not task.get("auto_pay"):
                    await query.edit_message_text(
                        f"⚠️ *确认开启自动付款*\n\n"
                        f"📦 型号: {friendly_plan_name(plan_code)} (`{plan_code}`)\n"
                        f"💾 配置: {format_memory(task.get('memory'))} + {format_storage(task.get('storage'))}\n\n"
                        f"监控抢购成功时，将使用 OVH 首选付款方式请求自动扣款。\n"
                        f"该设置只对当前监控任务生效。",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("⚠️ 确认开启自动付款", callback_data=f"watchlist|autopayconfirm|{task_id}")],
                            [InlineKeyboardButton("⬅️ 返回任务", callback_data=f"watchlist|task|{task_id}"), InlineKeyboardButton("取消", callback_data="cancel")],
                        ]),
                    )
                    return
                task["auto_pay"] = parts[1] == "autopayconfirm"
                task["chat_id"] = str(query.message.chat_id)
                save_watch_tasks()
                await query.answer("自动付款已开启" if task["auto_pay"] else "自动付款已关闭")
                parts = ["watchlist", "task", task_id]
                action_btn = InlineKeyboardButton(
                    "⏸ 暂停监控" if task.get("active") else "▶️ 启用监控",
                    callback_data=f"watchlist|toggle|{task_id}",
                )
                mode_btn = InlineKeyboardButton(
                    "🔔 改为仅通知" if watch_auto_buy_enabled(task) else "🚀 改为自动下单",
                    callback_data=f"watchlist|mode|{task_id}",
                )
                keyboard = InlineKeyboardMarkup([
                    [action_btn], [mode_btn],
                    [InlineKeyboardButton("📍 修改监控机房", callback_data=f"watchlist|dcs|{task_id}")],
                    [InlineKeyboardButton(
                        "💳 关闭自动付款" if task.get("auto_pay") else "💳 开启自动付款",
                        callback_data=f"watchlist|autopay|{task_id}",
                    )],
                    [InlineKeyboardButton("🎯 重新设置下单数量", callback_data=f"watchlist|count|{task_id}")],
                    [InlineKeyboardButton("🗑 删除监控", callback_data=f"watchlist|delete|{task_id}")],
                    [InlineKeyboardButton("⬅️ 返回任务列表", callback_data="watchlist|manage"), InlineKeyboardButton("取消", callback_data="cancel")],
                ])
                await query.edit_message_text(
                    f"⚙️ 管理监控任务\n\n{format_watchlist_task(plan_code, task)}",
                    parse_mode="Markdown", reply_markup=keyboard,
                )
                return

            if len(parts) >= 3 and parts[1] in {"dcs", "dctoggle"}:
                task_id = parts[2]
                task = watch_tasks.get(task_id)
                plan_code = task.get("plan_code", task_id) if task else task_id
                if not task:
                    await query.edit_message_text("❌ 监控任务不存在或已删除")
                    return
                all_configs = await asyncio.to_thread(ovh_client.check_availability, plan_code)
                selected_cfg = next(
                    (cfg for cfg in all_configs if task.get("fqn") and cfg.get("fqn") == task.get("fqn")),
                    None,
                )
                if not selected_cfg:
                    selected_cfg = next(
                        (cfg for cfg in all_configs
                         if memory_matches(cfg.get("memory"), task.get("memory"))
                         and storage_matches(cfg.get("storage"), task.get("storage"))),
                        None,
                    )
                if not selected_cfg:
                    await query.edit_message_text("❌ 无法读取该配置支持的机房，请稍后重试")
                    return
                task["fqn"] = selected_cfg.get("fqn")
                all_dcs = list(selected_cfg.get("datacenters", {}).keys())
                if task.get("dc"):
                    monitored = {task["dc"]}
                else:
                    monitored = set(all_dcs) - set(task.get("excluded_dcs", []))
                if parts[1] == "dctoggle" and len(parts) >= 4:
                    dc = parts[3]
                    if dc in monitored:
                        if len(monitored) <= 1:
                            await query.answer("至少保留一个监控机房", show_alert=True)
                        else:
                            monitored.remove(dc)
                    else:
                        monitored.add(dc)
                    task["dc"] = None
                    task["excluded_dcs"] = sorted(set(all_dcs) - monitored)
                    task["chat_id"] = str(query.message.chat_id)
                    save_watch_tasks()
                keyboard = []
                for dc, status in selected_cfg.get("datacenters", {}).items():
                    mark = "✅" if dc in monitored else "❌"
                    keyboard.append([InlineKeyboardButton(
                        f"{mark} {format_dc(dc)} ({format_dc_status(status)})",
                        callback_data=f"watchlist|dctoggle|{task_id}|{dc}",
                    )])
                keyboard.append([
                    InlineKeyboardButton("⬅️ 返回任务", callback_data=f"watchlist|task|{task_id}"),
                    InlineKeyboardButton("取消", callback_data="cancel"),
                ])
                await query.edit_message_text(
                    f"📍 *修改监控机房*\n\n"
                    f"📦 型号: {friendly_plan_name(plan_code)} (`{plan_code}`)\n"
                    f"💾 配置: {format_memory(task.get('memory'))} + {format_storage(task.get('storage'))}\n\n"
                    f"✅ 正在监控　❌ 已取消\n点击机房即可添加或取消监控。",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
                return

            if len(parts) >= 3 and parts[1] == "count":
                task_id = parts[2]
                task = watch_tasks.get(task_id)
                plan_code = task.get("plan_code", task_id) if task else task_id
                if not task:
                    await query.edit_message_text("❌ 监控任务不存在或已删除")
                    return
                context.user_data["watch_count_edit"] = {
                    "task_id": task_id,
                    "message": query.message,
                }
                await query.edit_message_text(
                    f"🎯 重新设置下单数量\n\n"
                    f"📦 型号: `{plan_code}`\n"
                    f"本轮进度: {task.get('ordered', 0)}/{task.get('max_orders', 1)} 单\n\n"
                    f"请直接发送新的下单数量。\n"
                    f"例如要从现在重新下 5 单，就发送：`5`\n"
                    f"设置后进度会重置为 0/5。\n"
                    f"可设置范围：1–100 单",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("⬅️ 返回任务", callback_data=f"watchlist|task|{task_id}"),
                        InlineKeyboardButton("取消", callback_data="cancel"),
                    ]]),
                )
                return

            if len(parts) >= 3 and parts[1] == "toggle":
                task_id = parts[2]
                task = watch_tasks.get(task_id)
                plan_code = task.get("plan_code", task_id) if task else task_id
                if not task:
                    await query.edit_message_text("❌ 监控任务不存在或已删除")
                    return
                was_active = task.get("active", True)
                task["active"] = not was_active
                task["chat_id"] = str(query.message.chat_id)
                if (
                    task["active"]
                    and watch_auto_buy_enabled(task)
                    and task.get("ordered", 0) >= task.get("max_orders", 1)
                ):
                    task["ordered"] = 0
                    task["_last_order_time"] = {}
                save_watch_tasks()
                if task["active"] and not watch_running:
                    watch_running = True
                    asyncio.ensure_future(watch_monitor_loop())
                status_text = "已启用" if task["active"] else "已暂停"
                await query.answer(f"{plan_code} {status_text}")
                parts = ["watchlist", "task", task_id]
                task = watch_tasks.get(task_id)
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
                    callback_data=f"watchlist|toggle|{task_id}"
                )
                mode_btn = InlineKeyboardButton(
                    "🔔 改为仅通知" if watch_auto_buy_enabled(task) else "🚀 改为自动下单",
                    callback_data=f"watchlist|mode|{task_id}"
                )
                keyboard = InlineKeyboardMarkup([
                    [action_btn],
                    [mode_btn],
                    [InlineKeyboardButton("📍 修改监控机房", callback_data=f"watchlist|dcs|{task_id}")],
                    [InlineKeyboardButton(
                        "💳 关闭自动付款" if task.get("auto_pay") else "💳 开启自动付款",
                        callback_data=f"watchlist|autopay|{task_id}",
                    )],
                    [InlineKeyboardButton("🎯 重新设置下单数量", callback_data=f"watchlist|count|{task_id}")],
                    [InlineKeyboardButton("🗑 删除监控", callback_data=f"watchlist|delete|{task_id}")],
                    [InlineKeyboardButton("⬅️ 返回任务列表", callback_data="watchlist|manage"), InlineKeyboardButton("取消", callback_data="cancel")],
                ])
                await query.edit_message_text(
                    f"⚙️ 管理监控任务\n\n{format_watchlist_task(plan_code, task)}",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
                return

            if len(parts) >= 3 and parts[1] == "mode":
                task_id = parts[2]
                task = watch_tasks.get(task_id)
                plan_code = task.get("plan_code", task_id) if task else task_id
                if not task:
                    await query.edit_message_text("❌ 监控任务不存在或已删除")
                    return
                task["auto_buy"] = not watch_auto_buy_enabled(task)
                task["chat_id"] = str(query.message.chat_id)
                if task["auto_buy"] and task.get("ordered", 0) >= task.get("max_orders", 1):
                    task["ordered"] = 0
                task["_last_order_time"] = {}
                save_watch_tasks()
                await query.answer(f"{plan_code} 已切换为{'自动下单' if task['auto_buy'] else '仅通知'}")
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
                    callback_data=f"watchlist|toggle|{task_id}"
                )
                mode_btn = InlineKeyboardButton(
                    "🔔 改为仅通知" if watch_auto_buy_enabled(task) else "🚀 改为自动下单",
                    callback_data=f"watchlist|mode|{task_id}"
                )
                keyboard = InlineKeyboardMarkup([
                    [action_btn],
                    [mode_btn],
                    [InlineKeyboardButton("📍 修改监控机房", callback_data=f"watchlist|dcs|{task_id}")],
                    [InlineKeyboardButton(
                        "💳 关闭自动付款" if task.get("auto_pay") else "💳 开启自动付款",
                        callback_data=f"watchlist|autopay|{task_id}",
                    )],
                    [InlineKeyboardButton("🎯 重新设置下单数量", callback_data=f"watchlist|count|{task_id}")],
                    [InlineKeyboardButton("🗑 删除监控", callback_data=f"watchlist|delete|{task_id}")],
                    [InlineKeyboardButton("⬅️ 返回任务列表", callback_data="watchlist|manage"), InlineKeyboardButton("取消", callback_data="cancel")],
                ])
                await query.edit_message_text(
                    f"⚙️ 管理监控任务\n\n{format_watchlist_task(plan_code, task)}",
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
                return

            if len(parts) >= 3 and parts[1] == "delete":
                task_id = parts[2]
                task = watch_tasks.get(task_id)
                plan_code = task.get("plan_code", task_id) if task else task_id
                if not task:
                    await query.edit_message_text("❌ 监控任务不存在或已删除")
                    return
                task["active"] = False
                del watch_tasks[task_id]
                save_watch_tasks()
                await query.answer(f"已删除 {plan_code}")
                if not watch_tasks:
                    await query.edit_message_text("📭 当前没有监控任务")
                    return
                keyboard = []
                for task_id, t in watch_tasks.items():
                    plan_code = t.get("plan_code", task_id)
                    status_icon = "🟢" if t.get("active") else "🔴"
                    keyboard.append([
                        InlineKeyboardButton(
                            f"{status_icon} {friendly_plan_name(plan_code)} · {format_storage(t.get('storage'))} ({t.get('ordered', 0)}/{t.get('max_orders', 1)})",
                            callback_data=f"watchlist|task|{task_id}"
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
                    f"📦 型号: `{plan_code}`\n"
                    f"💾 配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}\n"
                    f"📍 机房: {dc_display}",
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
                    f"📦 型号: `{plan_code}`\n"
                    f"💾 配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}\n"
                    f"📍 机房: {dc_display}\n"
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
                # 原子领取确认动作，防止 Telegram 重复回调造成重复下单。
                claimed_action = pending_actions.pop(action_id, None)
                if claimed_action is None:
                    await query.edit_message_text("❌ 该抢购操作已在执行，请勿重复点击")
                    return
                action = claimed_action
                plan_code = action["plan_code"]
                server_type = guess_server_type(plan_code)
                dc = action.get("dc")

                # 先检查有没有货，没货就不浪费时间调用下单 API
                available = await asyncio.to_thread(
                    ovh_client.find_available_configs,
                    plan_code,
                    target_dc=dc,
                    target_storage=action.get("storage"),
                    target_memory=action.get("memory"),
                )
                if not available:
                    dc_display = format_dc(dc) if dc else "全部机房"
                    cfg_mem = format_memory(action.get("memory", ""))
                    cfg_stor = format_storage(action.get("storage", ""))
                    await query.edit_message_text(
                        f"❌ *当前无货，无法抢购*\n\n"
                        f"📦 型号: `{plan_code}`\n"
                        f"💾 配置: {cfg_mem} + {cfg_stor}\n"
                        f"📍 机房: {dc_display}\n\n"
                        f"💡 请用 `/watch` 设定监控，等有货后自动下单",
                        parse_mode="Markdown"
                    )
                    return

                dc_display = format_dc(dc) if dc else available[0]["datacenter"]
                await query.edit_message_text(f"🚀 正在抢购 `{plan_code}` @ {dc_display}...")
                requested_count = max(1, min(int(action.get("count", 1)), 10))
                async with order_lock:
                    results = await asyncio.to_thread(
                        execute_buy_batch,
                        ovh_client,
                        requested_count,
                        plan_code=plan_code,
                        server_type=server_type,
                        datacenter=dc,
                        target_storage=action.get("storage"),
                        target_memory=action.get("memory"),
                    )

                if requested_count == 1:
                    text = _format_buy_result(results[0])
                else:
                    sections = [
                        f"*第 {index}/{requested_count} 单*\n{_format_buy_result(result)}"
                        for index, result in enumerate(results, 1)
                    ]
                    succeeded = sum(bool(result.get("success")) for result in results)
                    text = "\n\n".join(sections)
                    text += f"\n\n📊 实际成功: {succeeded}/{requested_count} 单"
                await query.edit_message_text(text, parse_mode="Markdown")

            elif action["type"] == "watch_start":
                try:
                    plan_code = action["plan_code"]
                    task_id = str(int(time.time() * 1000000))[-14:]
                    watch_tasks[task_id] = {
                        "plan_code": plan_code,
                        "fqn": action.get("fqn"),
                        "dc": action.get("dc"),
                        "excluded_dcs": action.get("excluded_dcs", []),
                        "storage": action.get("storage"),
                        "memory": action.get("memory"),
                        "auto_buy": action.get("auto_buy", True),
                        "auto_pay": False,
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
                        f"⚙️ 模式: {watch_mode_label(action)}\n"
                        f"🎯 下单上限: {action.get('max_orders', 1)}\n"
                        f"📊 已下: 0 单\n\n"
                        + (f"💡 达到上限后自动停止" if action.get("auto_buy", True) else f"💡 仅发送有货通知，不会自动下单"),
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.error(f"启动监控失败: {e}\n{traceback.format_exc()}")
                    await query.edit_message_text(
                        f"❌ 启动监控失败，操作未过期，可重试确认按钮或重新 /watch\n\n`{e}`",
                        parse_mode="Markdown"
                    )

            elif action["type"] == "reinstall":
                claimed_action = pending_actions.pop(action_id, None)
                if claimed_action is None:
                    await query.edit_message_text("❌ 安装操作已在执行，请勿重复点击")
                    return
                action = claimed_action
                service_name = action["service_name"]
                ip_address = action.get("ip", "")
                template = action["template"]
                hostname = action.get("hostname")
                ssh_key_name = action.get("ssh_key_name")
                raid0 = action.get("raid0", False)
                raid_disks = action.get("raid_disks")
                disk_group_id = action.get("disk_group_id")
                data_raid0 = action.get("data_raid0", False)
                data_disk_group_id = action.get("data_disk_group_id")
                data_raid_disks = action.get("data_raid_disks")
                await query.edit_message_text(
                    format_quick_install_progress(
                        service_name,
                        ip_address,
                        2,
                        "向 OVH 提交安装任务",
                        "最长等待 45 秒，期间每 5 秒刷新",
                    ) if action.get("quick_install") else (
                        f"⏳ 正在安装 `{template}` 到 `{service_name}`...\n"
                        "最长等待 OVH 响应 45 秒"
                    ),
                    parse_mode="Markdown",
                )

                async def update_install_submit_wait(elapsed: int):
                    if not action.get("quick_install"):
                        await query.edit_message_text(
                            f"⏳ 正在向 OVH 提交 `{template}` 安装任务...\n\n"
                            f"服务器: `{service_name}`\n已等待: {elapsed} 秒 / 最长 45 秒",
                            parse_mode="Markdown",
                        )
                        return
                    await query.edit_message_text(
                        format_quick_install_progress(
                            service_name,
                            ip_address,
                            min(4, 2 + elapsed // 15),
                            "等待 OVH 接收安装任务",
                            f"已等待 {elapsed} 秒 / 最长 45 秒",
                        ),
                        parse_mode="Markdown",
                    )

                try:
                    result = await run_ovh_call_with_heartbeat(
                        ovh_client.reinstall_server,
                        service_name, template, hostname,
                        timeout=45,
                        heartbeat=5,
                        on_wait=update_install_submit_wait,
                        ssh_key_name=ssh_key_name, raid0=raid0,
                        raid_disks=raid_disks, disk_group_id=disk_group_id,
                        data_raid0=data_raid0, data_disk_group_id=data_disk_group_id,
                        data_raid_disks=data_raid_disks,
                    )
                    task_id = result.get("taskId", "?") if isinstance(result, dict) else "?"
                    order_id = (result.get("orderId") or result.get("order_id")) if isinstance(result, dict) else None
                    raid_text = action.get("raid_text")
                    if not raid_text and data_raid0:
                        raid_text = f"系统盘 group={disk_group_id} + /data RAID0 group={data_disk_group_id}" + (f" disks={data_raid_disks}" if data_raid_disks else "")
                    elif not raid_text and raid0:
                        raid_text = f"RAID0 group={disk_group_id}" + (f" disks={raid_disks}" if raid_disks else "")
                    elif not raid_text and disk_group_id is not None:
                        raid_text = f"系统盘 group={disk_group_id} / 无 RAID0"
                    elif not raid_text:
                        raid_text = "默认分区 / 无 RAID"
                    await query.edit_message_text(
                        f"💿 *系统安装进度*\n\n"
                        f"🖥️ 服务器: `{service_name}`\n"
                        + (f"🌐 IP: `{ip_address}`\n" if ip_address else "")
                        + f"💿 系统: `{template}`\n"
                        + (f"🔑 SSH密钥: `{ssh_key_name}`\n" if ssh_key_name else "")
                        + f"🧩 磁盘: `{raid_text}`\n"
                        + f"📋 任务ID: `{task_id}`\n"
                        + (f"🧾 订单号: `{order_id}`\n" if order_id else "")
                        + "\n"
                        + f"`█░░░░░░░░░░░` 5%\n"
                        + f"📌 状态: `安装任务已提交`\n"
                        + f"⏱️ 耗时: 0分0秒\n\n"
                        + f"⏳ Bot 会自动刷新此进度。",
                        parse_mode="Markdown"
                    )
                    asyncio.ensure_future(
                        track_install_progress(
                            query.message, service_name, template, str(task_id),
                            ssh_key_name, raid_text, str(order_id) if order_id else None,
                            ip_address,
                        )
                    )
                except Exception as e:
                    running_task = parse_running_reinstall_task(e)
                    if running_task:
                        running_task_id, running_status = running_task
                        raid_text = action.get("raid_text") or (
                            f"RAID0 group={disk_group_id}"
                            if raid0 else
                            f"系统盘 group={disk_group_id} / 无 RAID0"
                            if disk_group_id is not None else
                            "默认分区 / 无 RAID"
                        )
                        await query.edit_message_text(
                            f"💿 *已接管正在运行的安装任务*\n\n"
                            f"🖥️ 服务器: `{service_name}`\n"
                            + (f"🌐 IP: `{ip_address}`\n" if ip_address else "")
                            + f"💿 系统: `{template}`\n"
                            + (f"🔑 SSH密钥: `{ssh_key_name}`\n" if ssh_key_name else "")
                            + f"🧩 磁盘: `{raid_text}`\n"
                            + f"📋 任务ID: `{running_task_id}`\n\n"
                            + f"`█░░░░░░░░░░░` 5%\n"
                            + f"📌 状态: `OVH 任务 {running_status}`\n"
                            + f"⏳ Bot 会自动刷新此进度。",
                            parse_mode="Markdown",
                        )
                        asyncio.ensure_future(
                            track_install_progress(
                                query.message, service_name, template,
                                running_task_id, ssh_key_name, raid_text,
                                None, ip_address,
                            )
                        )
                    else:
                        pending_actions[action_id] = action
                        await query.edit_message_text(
                            f"❌ 安装请求失败: `{e}`\n\n可确认重试，或取消后重新 /servers。",
                            parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup([[
                                InlineKeyboardButton("🔄 重试安装", callback_data=f"act|{action_id}"),
                                InlineKeyboardButton("取消", callback_data="cancel"),
                            ]]),
                        )

            elif action["type"] == "rescue_boot":
                service_name = action["service_name"]
                await query.edit_message_text(f"⏳ 正在设置 `{service_name}` 从 Rescue 启动...")
                try:
                    await asyncio.to_thread(
                        ovh_client.set_rescue_boot,
                        service_name, action["boot_id"],
                        action.get("public_key"), action.get("rescue_mail"),
                    )
                    await asyncio.to_thread(ovh_client.reboot_server, service_name)
                    pending_actions.pop(action_id, None)
                    auth_text = (
                        f"SSH 密钥 `{action.get('key_name')}`"
                        if action.get("public_key")
                        else f"密码将发送到 `{action.get('rescue_mail')}`"
                    )
                    await query.edit_message_text(
                        f"✅ *Rescue 启动指令已发送*\n\n"
                        f"🖥️ 服务器: `{service_name}`\n"
                        f"🔐 认证: {auth_text}\n"
                        f"⏳ 服务器正在重启，稍后将从 Rescue 系统启动。",
                        parse_mode="Markdown",
                    )
                except Exception as exc:
                    await query.edit_message_text(
                        f"❌ Rescue 启动失败: {exc}",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("🔄 重试", callback_data=f"act|{action_id}"),
                            InlineKeyboardButton("取消", callback_data="cancel"),
                        ]]),
                    )

            elif action["type"] == "reboot":
                service_name = action["service_name"]
                await query.edit_message_text(f"⏳ 正在重启 `{service_name}`...")
                try:
                    await asyncio.to_thread(ovh_client.reboot_server, service_name)
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

    async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await execute_callback_safely(_button_callback_impl, update, context)

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理交互输入或转发消息。"""
        nonlocal watch_running
        if not check_user(update.effective_user.id):
            return

        text = update.message.text or ""
        if not text.strip():
            return

        try:
            queried_ip = str(ipaddress.ip_address(text.strip()))
        except ValueError:
            queried_ip = None
        if queried_ip:
            records = await asyncio.to_thread(find_server_marks_by_ip, queried_ip)
            if not records:
                result_text = f"🔍 IP `{queried_ip}` 没有本地标记记录"
            else:
                lines = [f"🔍 *IP 标记记录*\n\n🌐 IP: `{queried_ip}`\n"]
                for index, record in enumerate(records, 1):
                    status = "🟢 当前仍标记" if record.get("active") else "⚪ 已清除"
                    lines.append(
                        f"{index}. 🖥️ `{record['service_name']}`\n"
                        f"   📝 {record['note']} · {status}\n"
                        f"   🕒 标记: {to_bjt(record['marked_at'])}"
                    )
                    if record.get("cleared_at"):
                        lines.append(f"   🧹 清除: {to_bjt(record['cleared_at'])}")
                result_text = "\n".join(lines)
            await update.message.reply_text(result_text, parse_mode="Markdown")
            try:
                await update.message.delete()
            except Exception:
                pass
            return

        rescue_mail_input = context.user_data.get("rescue_mail")
        if rescue_mail_input:
            email = text.strip()
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                await update.message.reply_text("❌ 邮箱格式无效，请重新发送")
                return
            action_id = rescue_mail_input["action_id"]
            action = pending_actions.get(action_id)
            if not action:
                context.user_data.pop("rescue_mail", None)
                await update.message.reply_text("❌ Rescue 操作已过期，请重新进入 `/servers`", parse_mode="Markdown")
                return
            confirm_id = str(int(time.time() * 1000))[-10:]
            pending_actions[confirm_id] = {
                "type": "rescue_boot", "service_name": action["service_name"],
                "boot_id": action.get("rescue_boot_id"),
                "public_key": None, "key_name": None, "rescue_mail": email,
            }
            context.user_data.pop("rescue_mail", None)
            await update.message.reply_text(
                f"⚠️ *确认从救援模式启动*\n\n"
                f"🖥️ 服务器: `{action['service_name']}`\n"
                f"✉️ 登录方式: OVH 将 Rescue 密码发送到 `{email}`\n\n"
                f"确认后会设置 Rescue 网络启动并立即重启服务器，当前业务将中断。",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚠️ 确认进入 Rescue", callback_data=f"act|{confirm_id}")],
                    [InlineKeyboardButton("取消", callback_data="cancel")],
                ]),
            )
            try:
                await update.message.delete()
            except Exception:
                pass
            return

        sshkey_add = context.user_data.get("sshkey_add")
        if sshkey_add:
            value = text.strip()
            if sshkey_add.get("stage") == "name":
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}", value):
                    await update.message.reply_text("❌ 名称只能包含字母、数字、点、下划线和短横线，最多 32 个字符，请重新发送")
                    return
                sshkey_add["name"] = value
                sshkey_add["stage"] = "key"
                await update.message.reply_text(
                    f"🔑 名称已记录：`{value}`\n\n请现在发送 SSH 公钥（以 `ssh-ed25519` 或 `ssh-rsa` 开头）。\n私钥内容会被拒绝。",
                    parse_mode="Markdown",
                )
                try:
                    await update.message.delete()
                except Exception:
                    pass
                return
            if value.startswith("-----BEGIN") or "PRIVATE KEY" in value.upper():
                await update.message.reply_text("❌ 检测到私钥内容。这里只能发送公钥，请重新发送。")
                return
            public_parts = value.split()
            if len(public_parts) < 2 or public_parts[0] not in {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521"}:
                await update.message.reply_text("❌ 公钥格式无效，请发送 ssh-ed25519、ssh-rsa 或 ecdsa 公钥。")
                return
            key_name = sshkey_add["name"]
            key_type = "rsa" if public_parts[0] == "ssh-rsa" else "ed25519"
            try:
                await asyncio.to_thread(ovh_client.create_ssh_key, key_name, value, key_type)
                context.user_data.pop("sshkey_add", None)
                keys = await asyncio.to_thread(ovh_client.list_ssh_keys)
                text_out = "✅ SSH 公钥已添加到 OVH\n\n🔑 *当前 SSH 密钥*\n\n" + "\n".join(f"• `{k}`" for k in keys)
                await update.message.reply_text(text_out, parse_mode="Markdown")
            except Exception as exc:
                await update.message.reply_text(f"❌ 添加 SSH 公钥失败: {exc}")
            try:
                await update.message.delete()
            except Exception:
                pass
            return

        create_count = context.user_data.get("watch_count_create")
        if create_count:
            session_id = create_count["session_id"]
            session = watch_sessions.get(session_id)
            if not session or not session.get("selected_cfg"):
                context.user_data.pop("watch_count_create", None)
                await update.message.reply_text("❌ 监控会话已过期，请重新 /watch")
                return
            value_text = text.strip()
            if not re.fullmatch(r"\d+", value_text):
                await update.message.reply_text("❌ 请输入纯数字，例如要下 5 单就发送：5")
                return
            requested = int(value_text)
            if requested < 1 or requested > 100:
                await update.message.reply_text("❌ 下单数量只能设置为 1–100，请重新发送")
                return
            session["max_orders"] = normalize_watch_round_orders(requested)
            context.user_data.pop("watch_count_create", None)
            cfg = session["selected_cfg"]
            dc = session.get("selected_dc")
            dc_display = "全部机房" if dc is None else format_dc(dc)
            price_line = await get_watch_untaxed_price_line(session, dc)
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 自动下单（默认）", callback_data=f"watch|mode|{session_id}|auto")],
                [InlineKeyboardButton("🔔 仅通知", callback_data=f"watch|mode|{session_id}|notify")],
                [InlineKeyboardButton("⬅️ 返回上一步", callback_data=f"watch|countback|{session_id}"), InlineKeyboardButton("取消", callback_data="cancel")],
            ])
            result_text = (
                f"⚙️ 选择监控模式\n\n"
                f"📦 型号: `{session['plan_code']}`\n"
                f"💾 配置: {format_memory(cfg['memory'])} + {format_storage(cfg['storage'])}\n"
                f"📍 机房: {dc_display}"
                f"{price_line}\n"
                f"🎯 下单数量: {session['max_orders']}"
            )
            prompt_message = create_count.get("message")
            try:
                await prompt_message.edit_text(result_text, parse_mode="Markdown", reply_markup=keyboard)
            except Exception as exc:
                logger.warning(f"更新 watch 数量消息失败，改为发送新消息: {exc}")
                await update.message.reply_text(result_text, parse_mode="Markdown", reply_markup=keyboard)
            try:
                await update.message.delete()
            except Exception:
                pass
            return

        count_edit = context.user_data.get("watch_count_edit")
        if count_edit:
            task_id = count_edit["task_id"]
            task = watch_tasks.get(task_id)
            plan_code = task.get("plan_code", task_id) if task else task_id
            if not task:
                context.user_data.pop("watch_count_edit", None)
                await update.message.reply_text("❌ 监控任务不存在或已删除")
                return
            value_text = text.strip()
            if not re.fullmatch(r"\d+", value_text):
                await update.message.reply_text("❌ 请输入纯数字，例如要下 5 单就发送：5")
                return
            requested = int(value_text)
            ordered = int(task.get("ordered", 0) or 0)
            if requested < 1 or requested > 100:
                await update.message.reply_text("❌ 下单数量只能设置为 1–100，请重新发送")
                return

            old_max = int(task.get("max_orders", 1) or 1)
            reached_old_limit = ordered >= old_max
            task["max_orders"] = normalize_watch_round_orders(requested)
            task["ordered"] = 0
            task["chat_id"] = str(update.effective_chat.id)
            task["_last_order_time"] = {}
            if reached_old_limit:
                task["active"] = True
            save_watch_tasks()
            if task.get("active") and not watch_running:
                watch_running = True
                asyncio.ensure_future(watch_monitor_loop())
            context.user_data.pop("watch_count_edit", None)

            status = "🟢 监控中" if task.get("active") else "🔴 已暂停"
            filter_parts = []
            filter_parts.append(f"机房={format_dc(task['dc'])}" if task.get("dc") else "机房=全部机房")
            if task.get("storage"):
                filter_parts.append(f"存储={format_storage(task['storage'])}")
            if task.get("memory"):
                filter_parts.append(f"内存={format_memory(task['memory'])}")
            action_btn = InlineKeyboardButton(
                "⏸ 暂停监控" if task.get("active") else "▶️ 启用监控",
                callback_data=f"watchlist|toggle|{task_id}",
            )
            mode_btn = InlineKeyboardButton(
                "🔔 改为仅通知" if watch_auto_buy_enabled(task) else "🚀 改为自动下单",
                callback_data=f"watchlist|mode|{task_id}",
            )
            keyboard = InlineKeyboardMarkup([
                [action_btn],
                [mode_btn],
                [InlineKeyboardButton("🎯 重新设置下单数量", callback_data=f"watchlist|count|{task_id}")],
                [InlineKeyboardButton("🗑 删除监控", callback_data=f"watchlist|delete|{task_id}")],
                [InlineKeyboardButton("⬅️ 返回任务列表", callback_data="watchlist|manage"), InlineKeyboardButton("取消", callback_data="cancel")],
            ])
            result_text = (
                f"✅ 已重新设置下单数量：{task['max_orders']} 单\n"
                f"本轮进度已重置为 0/{task['max_orders']}\n\n"
                f"⚙️ 管理监控任务\n\n{format_watchlist_task(plan_code, task)}"
            )
            prompt_message = count_edit.get("message")
            try:
                await prompt_message.edit_text(result_text, parse_mode="Markdown", reply_markup=keyboard)
            except Exception as exc:
                logger.warning(f"更新监控数量消息失败，改为发送新消息: {exc}")
                await update.message.reply_text(result_text, parse_mode="Markdown", reply_markup=keyboard)
            try:
                await update.message.delete()
            except Exception:
                pass
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

        async with order_lock:
            result = await asyncio.to_thread(
                ovh_client.quick_buy,
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

            # 指定配置无货时保持消息简短，不展开全部配置和机房状态。
            error_text = str(result.get("error", ""))
            is_out_of_stock = "无货" in error_text or "unavailable" in error_text.lower()
            if result.get("all_configs") and not is_out_of_stock:
                text += "\n\n📊 *所有配置状态:*\n"
                for cfg in result["all_configs"]:
                    mem = format_memory(cfg["memory"])
                    stor = format_storage(cfg["storage"])
                    text += f"  {mem} + {stor}:\n"
                    for dc, status in cfg["datacenters"].items():
                        icon = "✅" if status not in UNAVAILABLE_STATES else "❌"
                        text += f"    {icon} {format_dc(dc)}: {format_dc_status(status)}\n"

        return text

    async def delete_command_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """命令处理完成后删除用户发送的 /命令 消息。"""
        if not update.message or not check_user(update.effective_user.id):
            return
        try:
            await update.message.delete()
        except Exception as exc:
            logger.warning(f"删除用户命令消息失败: {exc}")

    async def restore_background_monitors(application):
        nonlocal watch_running, restock_running
        try:
            existing_servers = await asyncio.to_thread(ovh_client.list_servers)
            server_ip_map = {item.get("name"): item.get("ip", "") for item in existing_servers}
            for service_name in server_notes:
                if get_server_note(service_name) == "没中" and server_ip_map.get(service_name):
                    await asyncio.to_thread(
                        record_server_mark, service_name, server_ip_map[service_name]
                    )
        except Exception as exc:
            logger.warning(f"迁移现有服务器标记历史失败: {exc}")
        await application.bot.set_my_commands([
            BotCommand("start", "开始使用"),
            BotCommand("help", "查看完整帮助"),
            BotCommand("buy", "立即购买服务器"),
            BotCommand("watch", "添加配置监控"),
            BotCommand("watchlist", "管理监控任务"),
            BotCommand("restock", "全机型补货通知"),
            BotCommand("check", "查看型号库存"),
            BotCommand("status", "查看订单"),
            BotCommand("pay", "获取付款链接"),
            BotCommand("servers", "管理服务器"),
            BotCommand("keys", "查看 SSH 密钥"),
            BotCommand("catalog", "查看服务器目录"),
        ])
        if delivery_state.get("enabled"):
            asyncio.create_task(delivery_notification_loop())
            logger.info("恢复新主机发货邮件通知")
        if restock_state.get("enabled"):
            restock_running = True
            asyncio.create_task(restock_monitor_loop())
            logger.info("恢复全机型补货通知")
        active_count = sum(1 for task in watch_tasks.values() if task.get("active"))
        if active_count:
            watch_running = True
            asyncio.create_task(watch_monitor_loop())
            logger.info(f"恢复 {active_count} 个监控任务，自动启动监控循环")

    # ---- 构建 Bot ----
    # OVH 请求较慢时仍允许 Telegram 按钮回调及时进入，避免 callback query 过期。
    app = (
        ApplicationBuilder()
        .token(bot_token)
        .concurrent_updates(4)
        .post_init(restore_background_monitors)
        .build()
    )
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
    app.add_handler(CommandHandler("restock", restock_cmd))
    app.add_handler(CommandHandler("servers", servers_cmd))
    app.add_handler(CommandHandler("keys", keys_cmd))
    app.add_handler(CommandHandler("reinstall", reinstall_cmd))
    app.add_handler(CommandHandler("reboot", reboot_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.COMMAND, delete_command_message), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

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
