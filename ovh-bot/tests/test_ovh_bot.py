import ast
import inspect
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class FakeAPIError(Exception):
    pass


fake_ovh = types.ModuleType("ovh")
fake_ovh.Client = lambda **kwargs: types.SimpleNamespace()
fake_ovh.exceptions = types.SimpleNamespace(APIError=FakeAPIError)
sys.modules.setdefault("ovh", fake_ovh)

fake_requests = types.ModuleType("requests")
fake_requests.post = lambda *args, **kwargs: None
sys.modules.setdefault("requests", fake_requests)

import bot
import monitor


class ConfigTests(unittest.TestCase):
    def test_standard_toml_parser_preserves_hashes_and_types(self):
        content = b'''\
[telegram]
bot_token = "abc#def"
allowed_users = [123, 456]
allow_all_users = false

[monitor]
interval = 15
auto_buy = true
'''
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.toml"
            path.write_bytes(content)
            parsed = bot.parse_toml_simple(str(path))

        self.assertEqual(parsed["telegram"]["bot_token"], "abc#def")
        self.assertEqual(parsed["telegram"]["allowed_users"], [123, 456])
        self.assertIs(parsed["monitor"]["auto_buy"], True)
        self.assertEqual(parsed["monitor"]["interval"], 15)

    def test_user_access_fails_closed(self):
        self.assertFalse(bot.is_user_allowed(123, []))
        self.assertTrue(bot.is_user_allowed(123, [123]))
        self.assertTrue(bot.is_user_allowed(123, [], allow_all_users=True))

    def test_environment_monitor_values_are_typed(self):
        env = {
            "MONITOR_AUTO_BUY": "true",
            "MONITOR_MAX_ORDERS": "3",
            "MONITOR_INTERVAL": "20",
        }
        with patch.object(bot, "CONFIG_PATHS", []), patch.dict(bot.os.environ, env, clear=True):
            cfg = bot.load_config()

        self.assertIs(cfg["monitor"]["auto_buy"], True)
        self.assertEqual(cfg["monitor"]["max_orders"], 3)
        self.assertEqual(cfg["monitor"]["interval"], 20)


class OVHCallTests(unittest.IsolatedAsyncioTestCase):
    def test_install_callback_does_not_read_shadowed_run_config(self):
        run_bot_tree = ast.parse(inspect.getsource(bot.run_bot))
        callback = next(
            node
            for node in ast.walk(run_bot_tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_button_callback_impl"
        )
        shadowed_default_reads = [
            node
            for node in ast.walk(callback)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "cfg"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "defaults"
        ]

        self.assertEqual(shadowed_default_reads, [])

    async def test_blocking_ovh_call_times_out_with_readable_error(self):
        with self.assertRaisesRegex(TimeoutError, "OVH API 0.01 秒无响应"):
            await bot.run_ovh_call(time.sleep, 0.05, timeout=0.01)

    async def test_long_ovh_call_emits_heartbeat_updates(self):
        elapsed_updates = []

        async def record_wait(elapsed):
            elapsed_updates.append(elapsed)

        result = await bot.run_ovh_call_with_heartbeat(
            time.sleep,
            0.04,
            timeout=0.2,
            heartbeat=0.01,
            on_wait=record_wait,
        )

        self.assertIsNone(result)
        self.assertGreaterEqual(len(elapsed_updates), 1)

    async def test_callback_exception_is_visible_to_user(self):
        class FakeMessage:
            async def reply_text(self, text):
                self.reply = text

        class FakeQuery:
            def __init__(self):
                self.message = FakeMessage()
                self.edited = None
                self.alert = None

            async def answer(self, text, show_alert=False):
                self.alert = (text, show_alert)

            async def edit_message_text(self, text):
                self.edited = text

        async def broken_handler(update, context):
            raise RuntimeError("test failure")

        query = FakeQuery()
        update = types.SimpleNamespace(callback_query=query)
        await bot.execute_callback_safely(broken_handler, update, None)

        self.assertEqual(query.alert, ("按钮处理失败", True))
        self.assertIn("RuntimeError", query.edited)
        self.assertIn("/servers", query.edited)


class DiskSelectionTests(unittest.TestCase):
    def test_disk_types_distinguish_ssd_and_hdd(self):
        self.assertEqual(bot.classify_disk_group({"diskType": "NVME"})[0], "ssd")
        self.assertEqual(bot.classify_disk_group({"diskType": "SATA SSD"})[0], "ssd")
        self.assertEqual(bot.classify_disk_group({"diskType": "SATA"})[0], "hdd")
        self.assertEqual(bot.classify_disk_group({"diskType": "SAS HDD"})[0], "hdd")

    def test_default_raid_group_never_combines_groups(self):
        groups = [
            {"diskGroupId": 1, "numberOfDisks": 2, "diskType": "NVME"},
            {"diskGroupId": 2, "numberOfDisks": 4, "diskType": "SATA"},
        ]

        selected = bot.select_default_raid_group(groups, default_group_id=2)

        self.assertIs(selected, groups[1])
        self.assertEqual(selected["diskType"], "SATA")

    def test_ssd_group_is_fallback_when_default_group_cannot_raid(self):
        groups = [
            {"diskGroupId": 1, "numberOfDisks": 1, "diskType": "SATA"},
            {"diskGroupId": 2, "numberOfDisks": 2, "diskType": "SSD"},
            {"diskGroupId": 3, "numberOfDisks": 2, "diskType": "HDD"},
        ]

        selected = bot.select_default_raid_group(groups, default_group_id=1)

        self.assertIs(selected, groups[1])

    def test_no_raid_quick_install_uses_one_default_group(self):
        groups = [
            {"diskGroupId": 1, "numberOfDisks": 2, "diskType": "NVME"},
            {"diskGroupId": 2, "numberOfDisks": 4, "diskType": "HDD"},
        ]

        selected = bot.select_default_system_group(groups, default_group_id=2)

        self.assertIs(selected, groups[1])

    def test_no_raid_quick_install_falls_back_to_ssd_group(self):
        groups = [
            {"diskGroupId": 3, "numberOfDisks": 2, "diskType": "HDD"},
            {"diskGroupId": 4, "numberOfDisks": 1, "diskType": "SSD"},
        ]

        selected = bot.select_default_system_group(groups, default_group_id=99)

        self.assertIs(selected, groups[1])

    def test_default_ssh_key_prefers_configuration_then_first_key(self):
        self.assertEqual(bot.select_default_ssh_key(["key-a", "key-b"], "key-b"), "key-b")
        self.assertEqual(bot.select_default_ssh_key(["key-a", "key-b"], "missing"), "key-a")
        self.assertIsNone(bot.select_default_ssh_key([]))

    def test_servers_without_valid_disks_are_filtered(self):
        self.assertEqual(bot.extract_installable_disk_groups({}), [])
        self.assertEqual(bot.extract_installable_disk_groups({"diskGroups": []}), [])
        self.assertEqual(
            bot.extract_installable_disk_groups({
                "diskGroups": [
                    {"diskGroupId": None, "numberOfDisks": 2},
                    {"diskGroupId": 1, "numberOfDisks": 0},
                    {"diskGroupId": 3, "numberOfDisks": "unknown"},
                    {"diskGroupId": 2, "numberOfDisks": 2, "diskType": "NVME"},
                ]
            }),
            [{"diskGroupId": 2, "numberOfDisks": 2, "diskType": "NVME"}],
        )


class ServerPaginationTests(unittest.TestCase):
    def test_running_reinstall_error_is_parsed_for_progress_takeover(self):
        error = (
            "Task 555524320 of type reinstallServer with status todo "
            "is already running on server ns3198824.ip-198-244-164.eu"
        )
        self.assertEqual(
            bot.parse_running_reinstall_task(error),
            ("555524320", "todo"),
        )
        self.assertIsNone(bot.parse_running_reinstall_task("unrelated error"))

    def test_servers_are_sorted_by_creation_time_newest_first(self):
        servers = [
            {"name": "ns300", "created_at": "2026-01-01T00:00:00+00:00"},
            {"name": "ns100", "created_at": "2026-03-01T00:00:00+00:00"},
            {"name": "ns200", "created_at": "2026-02-01T00:00:00+00:00"},
        ]
        ordered = bot.sort_servers_newest_first(servers)
        self.assertEqual([item["name"] for item in ordered], ["ns100", "ns200", "ns300"])

    def test_exact_service_creation_time_wins_within_same_day(self):
        servers = [
            {
                "name": "older-high-source-index", "created_at": "2026-08-05",
                "exact_created_at": "2026-08-05T10:04:47Z", "_source_index": 99,
            },
            {
                "name": "newer-low-source-index", "created_at": "2026-08-05",
                "exact_created_at": "2026-08-05T16:01:38Z", "_source_index": 1,
            },
        ]
        ordered = bot.sort_servers_newest_first(servers)
        self.assertEqual(ordered[0]["name"], "newer-low-source-index")

    def test_same_day_servers_use_latest_source_position_first(self):
        servers = [
            {"name": "ns900", "created_at": "2026-08-04", "_source_index": 0},
            {"name": "ns100", "created_at": "2026-08-04", "_source_index": 1},
            {"name": "ns500", "created_at": "2026-08-04", "_source_index": 2},
        ]
        ordered = bot.sort_servers_newest_first(servers)
        self.assertEqual([item["name"] for item in ordered], ["ns500", "ns100", "ns900"])

    def test_servers_without_creation_time_use_name_number_fallback(self):
        servers = [
            {"name": "ns100", "created_at": ""},
            {"name": "ns300", "created_at": ""},
            {"name": "ns200", "created_at": ""},
        ]
        ordered = bot.sort_servers_newest_first(servers)
        self.assertEqual([item["name"] for item in ordered], ["ns300", "ns200", "ns100"])

    def test_servers_listing_does_not_use_install_api_timeout(self):
        source = Path(bot.__file__).read_text(encoding="utf-8")
        servers_start = source.index("    async def servers_cmd")
        servers_end = source.index("    async def keys_cmd", servers_start)
        servers_source = source[servers_start:servers_end]

        self.assertIn("asyncio.to_thread(ovh_client.list_servers)", servers_source)
        self.assertIn("asyncio.to_thread(ovh_client.get_server_hardware", servers_source)
        self.assertNotIn("run_ovh_call(", servers_source)

    def test_pages_respect_character_and_item_limits(self):
        entries = [
            {"text": f"server-{index}\n" + ("x" * 70), "keyboard": [[index]]}
            for index in range(7)
        ]

        pages = bot.paginate_server_entries(entries, max_chars=250, max_items=2)

        self.assertEqual([len(page) for page in pages], [2, 2, 2, 1])
        for page in pages:
            self.assertLessEqual(sum(len(entry["text"]) for entry in page) + 2 * (len(page) - 1), 250)

    def test_oversized_server_is_truncated_on_complete_lines(self):
        entry = {
            "text": "server\n" + "\n".join(f"`disk-{index}`" for index in range(100)),
            "keyboard": [["install"]],
        }

        page = bot.paginate_server_entries([entry], max_chars=180, max_items=4)[0]

        self.assertLessEqual(len(page[0]["text"]), 180)
        self.assertIn("其余详情已省略", page[0]["text"])
        self.assertEqual(page[0]["keyboard"], [["install"]])

    def test_server_list_selects_before_offering_quick_install(self):
        rows = bot.server_list_action_specs(
            2,
            "srv2_123456",
        )
        callbacks = [button["callback_data"] for row in rows for button in row]
        labels = [button["text"] for row in rows for button in row]

        self.assertEqual(callbacks[0], "srv|select|srv2_123456")
        self.assertIn("选择 2", labels[0])
        self.assertNotIn("srv|quick|srv2_123456", callbacks)
        self.assertNotIn("清除", "".join(labels))

        selected_rows = bot.selected_server_action_specs("srv2_123456")
        selected_callbacks = [
            button["callback_data"] for row in selected_rows for button in row
        ]
        self.assertEqual(selected_callbacks, [
            "srv|quick|srv2_123456",
            "srv|quick_noraid|srv2_123456",
            "srv|install|srv2_123456",
            "srvnote|miss|srv2_123456",
        ])

        selected_with_note = bot.selected_server_action_specs(
            "srv2_123456", has_miss_note=True
        )
        selected_with_note_callbacks = [
            button["callback_data"] for row in selected_with_note for button in row
        ]
        self.assertEqual(selected_with_note_callbacks[-1], "srvnote|clear|srv2_123456")
        self.assertIn("清除", selected_with_note[-1][0]["text"])

        no_raid_rows = bot.selected_server_action_specs(
            "srv2_123456", quick_available=False
        )
        no_raid_callbacks = [
            button["callback_data"] for row in no_raid_rows for button in row
        ]
        self.assertEqual(no_raid_callbacks, [
            "srv|quick_noraid|srv2_123456",
            "srv|install|srv2_123456",
            "srvnote|miss|srv2_123456",
        ])

    def test_multiple_server_note_buttons_have_independent_persistent_targets(self):
        first = bot.server_note_callback_data(
            "miss", "ns111111.ip-192-0-2.eu", "finish"
        )
        second = bot.server_note_callback_data(
            "miss", "ns222222.ip-192-0-2.eu", "finish"
        )

        self.assertNotEqual(first, second)
        self.assertEqual(
            bot.parse_server_note_callback(first),
            ("finish", "miss", "ns111111.ip-192-0-2.eu"),
        )
        self.assertEqual(
            bot.parse_server_note_callback(second),
            ("finish", "miss", "ns222222.ip-192-0-2.eu"),
        )
        self.assertLessEqual(len(first.encode("utf-8")), 64)

    def test_system_choices_do_not_require_compatible_templates_api(self):
        choices = bot.reinstall_template_choices("debian12_64")

        self.assertEqual(choices[0], {
            "template": "debian12_64",
            "label": "Debian 12 (默认)",
        })
        self.assertIn("ubuntu2404-server_64", [item["template"] for item in choices])

    def test_quick_install_progress_shows_server_ip_stage_and_percent(self):
        text = bot.format_quick_install_progress(
            "ns123456.ip-192-0-2.eu",
            "192.0.2.10",
            30,
            "读取 OVH SSH 密钥",
            "步骤 2/4 · 最长等待 20 秒",
        )

        self.assertIn("`ns123456.ip-192-0-2.eu`", text)
        self.assertIn("`192.0.2.10`", text)
        self.assertIn("30%", text)
        self.assertIn("读取 OVH SSH 密钥", text)
        self.assertIn("最长等待 20 秒", text)

    def test_request_task_activity_does_not_replace_install_activity(self):
        status, percent, done, activity_seen = bot.reconcile_submitted_install_progress(
            "安装步骤已完成",
            100,
            True,
            "doing",
            "debian12_64",
            False,
        )

        self.assertFalse(done)
        self.assertLess(percent, 100)
        self.assertFalse(activity_seen)
        self.assertIn("doing", status)

    def test_previous_completed_status_waits_for_new_install_activity(self):
        status, percent, done, activity_seen = bot.reconcile_submitted_install_progress(
            "安装已结束或 OVH 暂无安装状态",
            100,
            True,
            "done",
            "debian12_64",
            False,
        )

        self.assertEqual(status, "等待本次安装流程开始，重装请求已受理")
        self.assertEqual(percent, 5)
        self.assertFalse(done)
        self.assertFalse(activity_seen)

    def test_request_task_done_does_not_finish_running_install(self):
        status, percent, done, activity_seen = bot.reconcile_submitted_install_progress(
            "等待下一步",
            75,
            False,
            "done",
            "debian12_64",
            True,
        )

        self.assertFalse(done)
        self.assertEqual(percent, 75)
        self.assertTrue(activity_seen)
        self.assertIn("重装请求已受理", status)

    def test_install_finishes_after_observed_activity_becomes_idle(self):
        status, percent, done, activity_seen = bot.reconcile_submitted_install_progress(
            "安装已结束或 OVH 暂无安装状态",
            100,
            True,
            "done",
            "debian12_64",
            True,
        )

        self.assertTrue(done)
        self.assertEqual(percent, 100)
        self.assertTrue(activity_seen)
        self.assertIn("debian12_64", status)


class QuickBuySafetyTests(unittest.TestCase):
    def test_config_price_can_return_monthly_and_installation_breakdown(self):
        client = bot.OVHClient({"ovh": {}})
        client.create_cart = lambda: {"cartId": "cart-1"}
        client.delete_cart = lambda cart_id: None
        client._find_addon_options = lambda *args: []
        client.get_catalog = lambda category: {"plans": [{
            "planCode": "24sk202",
            "pricings": [{
                "mode": "default", "capacities": ["installation"], "phase": 0,
                "price": 3799000000, "formattedPrice": "€ 37.99",
            }],
        }]}
        client.post = lambda path, **kwargs: {
            "itemId": 1,
            "prices": [{"label": "TOTAL", "price": {
                "value": 37.99, "currencyCode": "EUR"
            }}],
        }
        client.get = lambda path, **kwargs: {"prices": {}}

        prices = client.get_config_price(
            "24sk202", "fra", "ram-32g", "softraid-2x450nvme",
            include_tax=False, breakdown=True,
        )

        self.assertEqual(prices["monthly"], 37.99)
        self.assertEqual(prices["installation"], 37.99)
        self.assertEqual(prices["currency"], "EUR")

    def test_watch_flow_displays_price_breakdown_and_field_icons(self):
        source = Path(bot.__file__).read_text(encoding="utf-8")
        self.assertIn("💰 价格:", source)
        self.assertIn("🔧 安装费:", source)
        self.assertIn("(一次性)", source)
        self.assertIn("📦 型号:", source)
        self.assertIn("💾 配置:", source)
        self.assertIn("📍 机房:", source)

    def test_batch_count_is_honored_and_stops_on_first_failure(self):
        results = [buy_result(True), buy_result(False, "sold out"), buy_result(True)]
        client = types.SimpleNamespace(
            calls=0,
            quick_buy=lambda **kwargs: results.pop(0),
        )

        batch = bot.execute_buy_batch(client, 3, plan_code="plan")

        self.assertEqual(len(batch), 2)
        self.assertTrue(batch[0]["success"])
        self.assertFalse(batch[1]["success"])

    def test_checkout_without_order_id_is_reported_as_failure(self):
        client = bot.OVHClient({
            "ovh": {
                "application_key": "ak",
                "application_secret": "secret",
                "consumer_key": "ck",
            },
            "defaults": {"auto_assign": True, "auto_checkout": True},
        })
        chosen = {
            "datacenter": "fra",
            "fqn": "plan.ram-32g.softraid-2x500nvme",
            "memory": "ram-32g",
            "storage": "softraid-2x500nvme",
            "memory_display": "32 GB",
            "storage_display": "2x500 GB NVMe",
        }
        client.find_available_configs = lambda *args, **kwargs: [chosen]
        client._find_addon_options = lambda *args, **kwargs: [chosen["memory"], chosen["storage"]]
        client.create_cart = lambda: {"cartId": "cart-1"}
        client.add_eco_server = lambda *args, **kwargs: None
        client.assign_cart = lambda *args, **kwargs: None
        client.get_cart_summary = lambda *args, **kwargs: {"prices": {}}
        client.checkout = lambda *args, **kwargs: {}

        result = client.quick_buy("plan", datacenter="fra")

        self.assertFalse(result["success"])
        self.assertIn("orderId", result["error"])
        self.assertEqual(result["cart_id"], "cart-1")

    def test_reinstall_raid0_requires_one_valid_group(self):
        client = bot.OVHClient({
            "ovh": {
                "application_key": "ak",
                "application_secret": "secret",
                "consumer_key": "ck",
            },
        })
        posted = {}
        client.post = lambda path, **body: posted.update({"path": path, "body": body}) or {"taskId": 1}

        with self.assertRaises(ValueError):
            client.reinstall_server("srv", "debian12_64", raid0=True, raid_disks=2)
        with self.assertRaises(ValueError):
            client.reinstall_server("srv", "debian12_64", raid0=True, disk_group_id=1, raid_disks=1)

        client.reinstall_server(
            "srv", "debian12_64", raid0=True, disk_group_id=2, raid_disks=4
        )

        storage = posted["body"]["storage"]
        self.assertEqual(len(storage), 1)
        self.assertEqual(storage[0]["diskGroupId"], 2)
        self.assertEqual(storage[0]["partitioning"]["disks"], 4)


class FakeMonitorClient:
    zone = "IE"
    subsidiary = "IE"

    def __init__(self, results):
        self.results = list(results)
        self.buy_calls = []

    def check_availability(self, plan_code):
        return [{
            "fqn": "plan.ram-64g.softraid-2x960nvme",
            "memory": "ram-64g",
            "storage": "softraid-2x960nvme",
            "datacenters": {"fra": "available"},
        }]

    def quick_buy(self, **kwargs):
        self.buy_calls.append(kwargs)
        return self.results.pop(0)


def buy_result(success, error=None):
    return {
        "success": success,
        "cart_id": "cart-1" if success else None,
        "order_id": 42 if success else None,
        "payment_url": "https://example.invalid/pay" if success else None,
        "price": None,
        "elapsed": 0.1,
        "error": error,
    }


class MonitorTests(unittest.TestCase):
    def make_monitor(self, *, max_orders=1, auto_buy=True):
        cfg = {
            "ovh": {},
            "telegram": {},
            "monitor": {
                "watch_list": ["plan"],
                "auto_buy": auto_buy,
                "max_orders": max_orders,
                "order_cooldown": 120,
            },
        }
        instance = monitor.AvailabilityMonitor(cfg)
        instance.send_telegram = lambda *args, **kwargs: True
        return instance

    def test_monitor_passes_exact_available_configuration(self):
        instance = self.make_monitor()
        fake_client = FakeMonitorClient([buy_result(True)])
        instance.client = fake_client

        instance._check_one("plan")

        self.assertEqual(len(fake_client.buy_calls), 1)
        call = fake_client.buy_calls[0]
        self.assertEqual(call["datacenter"], "fra")
        self.assertEqual(call["target_memory"], "ram-64g")
        self.assertEqual(call["target_storage"], "softraid-2x960nvme")

    def test_failed_attempt_retries_only_after_cooldown(self):
        instance = self.make_monitor()
        fake_client = FakeMonitorClient([buy_result(False, "temporary"), buy_result(True)])
        instance.client = fake_client

        with patch.object(monitor.time, "time", return_value=1000):
            instance._check_one("plan")
        with patch.object(monitor.time, "time", return_value=1050):
            instance._check_one("plan")
        with patch.object(monitor.time, "time", return_value=1121):
            instance._check_one("plan")

        self.assertEqual(len(fake_client.buy_calls), 2)
        self.assertEqual(instance.orders_placed, 1)

    def test_successful_orders_stop_at_limit(self):
        instance = self.make_monitor(max_orders=1)
        fake_client = FakeMonitorClient([buy_result(True), buy_result(True)])
        instance.client = fake_client

        with patch.object(monitor.time, "time", return_value=1000):
            instance._check_one("plan")
        with patch.object(monitor.time, "time", return_value=1201):
            instance._check_one("plan")

        self.assertEqual(len(fake_client.buy_calls), 1)
        self.assertEqual(instance.orders_placed, 1)


class WatchTaskModeTests(unittest.TestCase):
    def test_watchlist_task_uses_friendly_multiline_layout(self):
        task = {
            "active": True,
            "auto_buy": True,
            "excluded_dcs": ["bhs", "waw"],
            "storage": "softraid-2x450nvme",
            "memory": "ram-32g-ecc",
            "ordered": 0,
            "max_orders": 5,
        }
        text = bot.format_watchlist_task("24sk202", task)
        lines = text.splitlines()
        self.assertEqual(lines[0], "🟢 监控中")
        self.assertEqual(lines[1], "📦 型号: KS-2 (24sk202)")
        self.assertTrue(lines[2].startswith("🚫 排除: "))
        self.assertIn("2x450GB NVMe", lines[3])
        self.assertIn("32GB", lines[3])
        self.assertTrue(lines[3].startswith("💾 配置: "))
        self.assertEqual(lines[4], "⚙️ 模式: 🚀 自动下单")
        self.assertEqual(lines[5], "📊 进度: 0/5 单")

    def test_restock_snapshot_only_reports_unavailable_to_available(self):
        rows_down = [{
            "planCode": "24sk202", "fqn": "24sk202.ram.storage",
            "memory": "ram-32g", "storage": "softraid-2x450nvme",
            "datacenters": [{"datacenter": "fra", "availability": "unavailable"}],
        }]
        rows_up = [{
            **rows_down[0],
            "datacenters": [{"datacenter": "fra", "availability": "available"}],
        }]
        previous = bot.build_restock_snapshot(rows_down, {"24sk202"})
        current = bot.build_restock_snapshot(rows_up, {"24sk202"})
        self.assertEqual(bot.find_restock_events({}, current), [])
        events = bot.find_restock_events(previous, current)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["dc"], "fra")

    def test_watch_tasks_use_independent_ids_and_keep_plan_code(self):
        source = Path(bot.__file__).read_text(encoding="utf-8")
        self.assertIn('watch_tasks[task_id] = {', source)
        self.assertIn('"plan_code": plan_code', source)
        self.assertNotIn('watch_tasks[plan_code] = {', source)
        self.assertIn('callback_data=f"watchlist|task|{task_id}"', source)

    def test_restock_notification_has_buy_button(self):
        source = Path(bot.__file__).read_text(encoding="utf-8")
        self.assertIn('callback_data=f"restockbuy|{buy_id}"', source)
        self.assertIn('CommandHandler("restock", restock_cmd)', source)

    def test_watchlist_can_toggle_datacenters_per_task(self):
        source = Path(bot.__file__).read_text(encoding="utf-8")
        self.assertIn('callback_data=f"watchlist|dcs|{task_id}"', source)
        self.assertIn('callback_data=f"watchlist|dctoggle|{task_id}|{dc}"', source)
        self.assertIn("至少保留一个监控机房", source)
        self.assertIn('task["excluded_dcs"] = sorted(set(all_dcs) - monitored)', source)

    def test_restock_is_registered_in_telegram_menu(self):
        source = Path(bot.__file__).read_text(encoding="utf-8")
        self.assertIn('BotCommand("restock", "全机型补货通知")', source)
        self.assertIn("await application.bot.set_my_commands", source)

    def test_delivery_email_subject_extracts_server(self):
        self.assertEqual(
            bot.parse_server_available_email(
                "[jd219982-ovh] Your ns31257627.ip-57-129-101.eu dedicated server is available!"
            ),
            "ns31257627.ip-57-129-101.eu",
        )
        self.assertIsNone(bot.parse_server_available_email("Order validated"))

    def test_delivery_monitor_uses_persistent_seen_ids(self):
        source = Path(bot.__file__).read_text(encoding="utf-8")
        self.assertIn('DELIVERY_FILE =', source)
        self.assertIn('ovh_client.get, "/me/notification/email/history"', source)
        self.assertIn("恢复新主机发货邮件通知", source)

    def test_delivery_notification_uses_two_entry_buttons(self):
        source = Path(bot.__file__).read_text(encoding="utf-8")
        self.assertIn('InlineKeyboardButton("🛠️ 安装系统"', source)
        self.assertIn('InlineKeyboardButton("📋 查看服务器"', source)
        self.assertIn('if parts[0] == "delivery"', source)
        self.assertIn('"📋 *服务器详情*\\n"', source)

    def test_watch_order_limit_resets_current_round(self):
        self.assertEqual(bot.normalize_watch_round_orders(5), 5)
        self.assertEqual(bot.normalize_watch_round_orders(0), 1)
        self.assertEqual(bot.normalize_watch_round_orders(999), 100)

    def test_watchlist_quantity_resets_progress_from_direct_input(self):
        source = Path(bot.__file__).read_text(encoding="utf-8")
        self.assertIn('context.user_data["watch_count_edit"]', source)
        self.assertIn('re.fullmatch(r"\\d+", value_text)', source)
        self.assertIn("请直接发送新的下单数量", source)
        self.assertIn('task["ordered"] = 0', source)
        self.assertIn('task["active"] = True', source)

    def test_watch_creation_quantity_uses_direct_number_input(self):
        source = Path(bot.__file__).read_text(encoding="utf-8")
        self.assertIn('context.user_data["watch_count_create"]', source)
        self.assertIn("请直接发送要下单的数量", source)
        self.assertNotIn('callback_data=f"watch|count|{session_id}|1"', source)

    def test_slash_command_hints_use_copyable_monospace(self):
        source = Path(bot.__file__).read_text(encoding="utf-8")
        self.assertIn('"用法: `/watch <planCode>`', source)
        self.assertIn('"示例: `/watch ks-1-b`', source)
        self.assertIn('"用法: `/buy <planCode>`', source)
        self.assertIn('示例: `/check ks-1-b`', source)

    def test_slash_command_messages_are_deleted(self):
        source = Path(bot.__file__).read_text(encoding="utf-8")
        self.assertIn("async def delete_command_message", source)
        self.assertIn("MessageHandler(filters.COMMAND, delete_command_message), group=1", source)

    def test_auto_buy_result_edits_original_progress_message(self):
        source = Path(bot.__file__).read_text(encoding="utf-8")
        monitor_start = source.index("    async def watch_monitor_loop")
        monitor_end = source.index("    async def _send_msg", monitor_start)
        monitor_source = source[monitor_start:monitor_end]

        self.assertIn("progress_message = await _send_msg", monitor_source)
        self.assertIn("await _edit_monitor_msg", monitor_source)

    def test_existing_tasks_default_to_auto_buy(self):
        self.assertTrue(bot.watch_auto_buy_enabled({}))
        self.assertEqual(bot.watch_mode_label({}), "🚀 自动下单")

    def test_notification_mode_is_explicit(self):
        task = {"auto_buy": False}
        self.assertFalse(bot.watch_auto_buy_enabled(task))
        self.assertEqual(bot.watch_mode_label(task), "🔔 仅通知")


if __name__ == "__main__":
    unittest.main()
