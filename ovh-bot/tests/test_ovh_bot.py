import sys
import tempfile
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

    def test_default_ssh_key_prefers_configuration_then_first_key(self):
        self.assertEqual(bot.select_default_ssh_key(["key-a", "key-b"], "key-b"), "key-b")
        self.assertEqual(bot.select_default_ssh_key(["key-a", "key-b"], "missing"), "key-a")
        self.assertIsNone(bot.select_default_ssh_key([]))


class ServerPaginationTests(unittest.TestCase):
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


class QuickBuySafetyTests(unittest.TestCase):
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
    def test_existing_tasks_default_to_auto_buy(self):
        self.assertTrue(bot.watch_auto_buy_enabled({}))
        self.assertEqual(bot.watch_mode_label({}), "🚀 自动下单")

    def test_notification_mode_is_explicit(self):
        task = {"auto_buy": False}
        self.assertFalse(bot.watch_auto_buy_enabled(task))
        self.assertEqual(bot.watch_mode_label(task), "🔔 仅通知")


if __name__ == "__main__":
    unittest.main()
