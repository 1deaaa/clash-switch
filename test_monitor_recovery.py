import unittest
import time
import pathlib
from unittest.mock import Mock, patch

from app import ConfigApp
from mihomo_client import MihomoClient, MihomoError
from monitor_service import (
    Monitor,
    MonitorConfig,
    _resolve_live_candidates,
    resolve_runtime_group,
    switch_to_best_verified_candidate,
)


class FakeClient:
    def __init__(self):
        self.selected = "当前节点"
        self.selections = []
        self.web_probes = []
        self.unavailable = {"当前节点"}
        self.candidates = [
            {"name": "候选甲", "provider": "新订阅"},
            {"name": "候选乙", "provider": "新订阅"},
        ]

    def selector_groups(self):
        return {"🤖AI网站": {"type": "Selector"}}

    def selector_group_snapshot(self, group):
        if group != "🤖AI网站":
            raise MihomoError(f"未知策略组：{group}")
        return self.selected, self.candidates

    def test_delay(self, proxy, url, timeout_ms, expected=""):
        return {"当前节点": 10, "候选甲": 20, "候选乙": 30}[proxy]

    def select(self, group, proxy):
        self.selections.append((group, proxy))
        self.selected = proxy

    def web_probe(self, url, timeout_ms, allowed_hosts, blocked_keywords):
        self.web_probes.append(self.selected)
        if self.selected in self.unavailable:
            raise MihomoError("出口地区不受支持")
        return {"status": 200, "final_url": url}


class FailureFirstClient(FakeClient):
    def test_delay(self, proxy, url, timeout_ms, expected=""):
        if proxy == "当前节点":
            raise MihomoError("当前节点不可达")
        time.sleep(0.03)
        return {"候选甲": 20, "候选乙": 30}[proxy]


class FakeRPCResponse:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, *_args):
        return self._body


class FakeRPCOpener:
    def __init__(self, response: FakeRPCResponse):
        self.response = response

    def open(self, *_args, **_kwargs):
        return self.response


class MonitorRecoveryTests(unittest.TestCase):
    def test_group_selection_projection_is_shared_by_node_name(self):
        app = object.__new__(ConfigApp)
        app.selected_candidates = {"重复节点", "仅甲组"}
        app._group_live_candidate_names = {
            "甲组": {"重复节点", "仅甲组"},
            "乙组": {"重复节点", "仅乙组"},
        }

        app._sync_group_selection_maps()

        self.assertEqual(app._group_selected_candidates["甲组"], {"重复节点", "仅甲组"})
        self.assertEqual(app._group_selected_candidates["乙组"], {"重复节点"})

    def test_missing_group_uses_unique_ai_selector(self):
        config = MonitorConfig(group="旧订阅", test_urls=["https://aistudio.google.com"])
        self.assertEqual(
            resolve_runtime_group(config, {"🤖AI网站": {"type": "Selector"}}),
            "🤖AI网站",
        )

    def test_changed_subscription_uses_all_live_candidates(self):
        configured = ["旧节点", "仍存在的旧节点"]
        live = [
            {"name": "仍存在的旧节点", "provider": "新订阅"},
            {"name": "新节点", "provider": "新订阅"},
        ]
        self.assertEqual(_resolve_live_candidates(configured, live), (["仍存在的旧节点", "新节点"], True))

    def test_nested_selector_snapshot_resolves_the_final_node(self):
        client = object.__new__(MihomoClient)
        client.proxies = Mock(
            return_value={
                "AI网站": {"type": "Selector", "now": "节点选择", "all": ["节点甲", "节点乙"]},
                "节点选择": {"type": "Selector", "now": "节点甲"},
                "节点甲": {"type": "VLESS"},
                "节点乙": {"type": "VLESS"},
            }
        )

        current, candidates = client.selector_group_snapshot("AI网站")

        self.assertEqual(current, "节点甲")
        self.assertEqual([item["name"] for item in candidates], ["节点甲", "节点乙"])

    def test_missing_group_prefers_largest_recursive_selector(self):
        proxies = {
            "小组": {"type": "Selector", "all": ["节点甲"]},
            "节点甲": {"type": "VLESS"},
            "大组": {"type": "Selector", "all": ["下级组"]},
            "下级组": {"type": "Selector", "all": ["节点乙", "节点丙"]},
            "节点乙": {"type": "VLESS"},
            "节点丙": {"type": "VLESS"},
        }
        groups = {name: item for name, item in proxies.items() if item.get("type") == "Selector"}
        config = MonitorConfig(group="已删除订阅", test_urls=["https://hub.docker.com/"])

        self.assertEqual(resolve_runtime_group(config, groups, proxies), "大组")

    def test_unreachable_current_node_switches_to_page_verified_candidate(self):
        config = MonitorConfig(
            test_urls=["https://aistudio.google.com"],
            timeout_ms=1000,
            failure_threshold=1,
            advanced_web_probe=True,
            group="旧订阅",
            candidates=["已删除的节点"],
        )
        client = FakeClient()
        with patch("monitor_service.MihomoClient", return_value=client):
            monitor = Monitor(config)

        result = monitor.check_once()

        self.assertEqual(result["status"], "switched")
        self.assertIn(result["selected"], {"候选甲", "候选乙"})
        self.assertIn(("🤖AI网站", result["selected"]), client.selections)
        self.assertEqual(client.web_probes[0], "当前节点")
        self.assertIn(result["selected"], client.web_probes)

    def test_switch_waits_for_slow_candidates_after_current_fails(self):
        config = MonitorConfig(
            test_urls=["https://aistudio.google.com"],
            timeout_ms=1000,
            failure_threshold=1,
            advanced_web_probe=True,
            group="🤖AI网站",
            candidates=["候选甲", "候选乙"],
        )
        client = FailureFirstClient()
        with patch("monitor_service.MihomoClient", return_value=client):
            monitor = Monitor(config)

        result = monitor.check_once()

        self.assertEqual(result["status"], "switched")
        self.assertEqual(result["selected"], "候选甲")

    def test_failed_candidate_page_is_skipped_for_next_cached_candidate(self):
        config = MonitorConfig(
            advanced_web_probe=True,
            group="🤖AI网站",
            candidates=["候选甲", "候选乙"],
        )
        client = FakeClient()
        client.unavailable.add("候选甲")
        with patch("monitor_service.MihomoClient", return_value=client):
            monitor = Monitor(config)
        monitor.node_cache = {
            "候选甲": {"status": "ok", "delay": 10, "checked_at": time.monotonic(), "round_id": 1},
            "候选乙": {"status": "ok", "delay": 20, "checked_at": time.monotonic(), "round_id": 1},
        }

        switched = monitor._switch_from_cache("当前节点", 2, config, {})

        self.assertEqual(switched["selected"], "候选乙")
        self.assertEqual(client.selections, [("🤖AI网站", "候选甲"), ("🤖AI网站", "候选乙")])

    def test_manual_verified_switch_skips_region_blocked_candidate(self):
        config = MonitorConfig(
            advanced_web_probe=True,
            group="🤖AI网站",
            candidates=["候选甲", "候选乙"],
        )
        client = FakeClient()
        client.unavailable.add("候选甲")
        cache = {
            "候选甲": {"status": "ok", "delay": 10, "checked_at": time.monotonic(), "round_id": 1},
            "候选乙": {"status": "ok", "delay": 20, "checked_at": time.monotonic(), "round_id": 1},
        }

        switched = switch_to_best_verified_candidate(client, config, "当前节点", cache, 1)

        self.assertEqual(switched["selected"], "候选乙")
        self.assertEqual(client.selected, "候选乙")

    def test_ai_studio_rpc_region_403_is_node_failure(self):
        client = object.__new__(MihomoClient)
        client._aistudio_api_keys = ()
        key = "AIza" + "A" * 24
        with (
            patch("mihomo_client.find_storage_state", return_value=pathlib.Path("/tmp/auth.json")),
            patch.object(MihomoClient, "_storage_cookie_header", return_value=("SID=x", "sid")),
            patch(
                "mihomo_client.urllib.request.build_opener",
                return_value=FakeRPCOpener(FakeRPCResponse(403, '[7,"Region not supported."]')),
            ),
        ):
            with self.assertRaises(MihomoError):
                client._aistudio_api_probe("http://127.0.0.1:7890", key, 1000)

    def test_ai_studio_rpc_key_service_403_is_not_node_failure(self):
        client = object.__new__(MihomoClient)
        client._aistudio_api_keys = ()
        key = "AIza" + "B" * 24
        with (
            patch("mihomo_client.find_storage_state", return_value=pathlib.Path("/tmp/auth.json")),
            patch.object(MihomoClient, "_storage_cookie_header", return_value=("SID=x", "sid")),
            patch(
                "mihomo_client.urllib.request.build_opener",
                return_value=FakeRPCOpener(
                    FakeRPCResponse(403, '[7,"API_KEY_SERVICE_BLOCKED"]')
                ),
            ),
        ):
            self.assertIsNone(client._aistudio_api_probe("http://127.0.0.1:7890", key, 1000))


if __name__ == "__main__":
    unittest.main()
