import unittest
import time
import pathlib
from unittest.mock import Mock, patch

from app import ConfigApp, filter_current_node_logs
from mihomo_client import (
    ControllerSettings,
    MihomoClient,
    MihomoError,
    MihomoGlobalProbeError,
    discover_settings,
)
from monitor_service import (
    Monitor,
    MonitorConfig,
    _resolve_live_candidates,
    _live_candidate_signature,
    cache_node_result,
    fresh_cached_candidates,
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


class GlobalProbeUnavailableClient(FakeClient):
    def web_probe(self, url, timeout_ms, allowed_hosts, blocked_keywords):
        from mihomo_client import MihomoGlobalProbeError

        raise MihomoGlobalProbeError("没有可用浏览器或自动登录态")


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

    def geturl(self):
        return "http://127.0.0.1/"


class FakeWebResponse(FakeRPCResponse):
    def __init__(self, status: int, body: str, final_url: str):
        super().__init__(status, body)
        self.final_url = final_url
        self._read_once = True

    def read(self, *_args):
        if not self._read_once:
            return b""
        self._read_once = False
        return self._body

    def geturl(self):
        return self.final_url


class FakeRPCOpener:
    def __init__(self, response: FakeRPCResponse):
        self.response = response

    def open(self, *_args, **_kwargs):
        return self.response


class MonitorRecoveryTests(unittest.TestCase):
    def test_discovery_prefers_connectable_runtime_socket_over_stale_configured_socket(self):
        stale = pathlib.Path("/tmp/stale-verge-mihomo.sock")
        runtime = pathlib.Path("/run/clash-verge-service/users/1000/verge-mihomo.sock")
        with (
            patch("mihomo_client.os.name", "posix"),
            patch("mihomo_client._verge_config_candidates", return_value=[]),
            patch("mihomo_client._is_socket", side_effect=lambda path: path in {stale, runtime}),
            patch("mihomo_client._runtime_socket_candidates", return_value=[runtime]),
            patch(
                "mihomo_client._is_connectable_unix_socket",
                side_effect=lambda path, timeout=0.2: path == runtime,
            ),
        ):
            settings = discover_settings(ControllerSettings(socket_path=str(stale)))

        self.assertEqual(settings.socket_path, str(runtime))

    def test_discovery_keeps_stale_configured_socket_during_restart_window(self):
        stale = pathlib.Path("/tmp/stale-verge-mihomo.sock")
        with (
            patch("mihomo_client.os.name", "posix"),
            patch("mihomo_client._verge_config_candidates", return_value=[]),
            patch("mihomo_client._is_socket", side_effect=lambda path: path == stale),
            patch("mihomo_client._runtime_socket_candidates", return_value=[]),
            patch("mihomo_client._is_connectable_unix_socket", return_value=False),
        ):
            settings = discover_settings(ControllerSettings(socket_path=str(stale)))

        self.assertEqual(settings.socket_path, str(stale))

    def test_live_signature_changes_when_same_name_endpoint_changes(self):
        candidates = [{"name": "同名节点", "provider": "订阅"}]
        first = _live_candidate_signature(candidates, {"同名节点": {"type": "Vless", "server": "1.1.1.1"}})
        second = _live_candidate_signature(candidates, {"同名节点": {"type": "Vless", "server": "2.2.2.2"}})

        self.assertNotEqual(first, second)

    def test_live_signature_ignores_runtime_proxy_health_fields(self):
        candidates = [{"name": "同名节点", "provider": "订阅"}]
        first = _live_candidate_signature(
            candidates,
            {
                "同名节点": {
                    "type": "Vless",
                    "server": "example.com",
                    "port": 443,
                    "alive": True,
                    "history": [{"time": "2026-10-02T00:00:00Z", "delay": 320}],
                    "extra": {"https://aistudio.google.com": [{"delay": 310}]},
                }
            },
        )
        second = _live_candidate_signature(
            candidates,
            {
                "同名节点": {
                    "type": "Vless",
                    "server": "example.com",
                    "port": 443,
                    "alive": False,
                    "history": [{"time": "2026-10-02T00:01:00Z", "delay": 920}],
                    "extra": {"https://aistudio.google.com": [{"delay": 880}]},
                }
            },
        )

        self.assertEqual(first, second)

    def test_current_node_log_includes_switch_event(self):
        lines = [
            "2026-01-01 [INFO] 已使用本轮缓存切换：旧节点 -> 新节点（300 ms，缓存 1.0 秒）",
        ]

        self.assertEqual(filter_current_node_logs(lines, "新节点"), lines)

    def test_failed_probe_keeps_recent_successful_cache(self):
        config = MonitorConfig(group="🤖AI网站", candidates=["候选甲"])
        cache = {}
        cache_node_result(cache, "候选甲", {"status": "ok", "delay": 20, "delays": {"u": 20}}, 1)
        cache_node_result(cache, "候选甲", {"status": "failed", "error": "HTTP 503"}, 2)

        available = fresh_cached_candidates(config, "当前节点", cache, round_id=2)

        self.assertEqual([(delay, name) for delay, name, _ in available], [(20, "候选甲")])
        self.assertEqual(available[0][2]["round_id"], 1)

    def test_non_selector_nested_groups_are_not_switchable_candidates(self):
        client = object.__new__(MihomoClient)
        client.proxies = Mock(
            return_value={
                "顶层": {"type": "Selector", "now": "直连节点", "all": ["自动组", "直连节点"]},
                "自动组": {"type": "URLTest", "now": "自动节点", "all": ["自动节点"]},
                "自动节点": {"type": "VLESS"},
                "直连节点": {"type": "VLESS"},
            }
        )

        _, candidates = client.selector_group_snapshot("顶层")

        self.assertEqual([item["name"] for item in candidates], ["直连节点"])
        with self.assertRaises(MihomoError):
            client.select_node("顶层", "自动节点")

    def test_global_browser_probe_failure_does_not_rotate_nodes(self):
        config = MonitorConfig(
            test_urls=["https://aistudio.google.com"],
            group="🤖AI网站",
            candidates=["候选甲", "候选乙"],
        )
        client = GlobalProbeUnavailableClient()
        with patch("monitor_service.MihomoClient", return_value=client):
            monitor = Monitor(config)

        result = monitor.check_once()

        self.assertEqual(result["status"], "probe_unavailable")
        self.assertEqual(client.selections, [])

    def test_group_selection_projection_is_shared_by_node_name(self):
        app = ConfigApp.__new__(ConfigApp)
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

    def test_empty_configured_candidates_mean_all_live_nodes(self):
        live = [{"name": "甲", "provider": "p"}, {"name": "乙", "provider": "p"}]
        self.assertEqual(_resolve_live_candidates([], live), (["甲", "乙"], True))

    def test_empty_group_selection_rotates_over_all_nodes(self):
        config = MonitorConfig(
            test_urls=["https://hub.docker.com/"],
            timeout_ms=1000,
            failure_threshold=1,
            advanced_web_probe=False,
            group="🤖AI网站",
            candidates=[],
        )
        client = FakeClient()
        client.selected = ""
        client.unavailable = set()
        with patch("monitor_service.MihomoClient", return_value=client):
            monitor = Monitor(config)

        result = monitor.check_once()

        self.assertEqual(result["status"], "switched")
        self.assertEqual(result["selected"], "候选甲")
        self.assertIn(("🤖AI网站", "候选甲"), client.selections)

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

    def test_ai_studio_login_redirect_is_global_failure(self):
        client = object.__new__(MihomoClient)
        client._aistudio_api_keys = ()
        opener = Mock()
        opener.open.return_value = FakeWebResponse(
            200,
            "<html>sign in</html>",
            "https://accounts.google.com/ServiceLogin?continue=https://aistudio.google.com",
        )
        with (
            patch.object(client, "request", return_value={"mixed-port": 7890}),
            patch("mihomo_client.urllib.request.build_opener", return_value=opener),
        ):
            with self.assertRaises(MihomoGlobalProbeError):
                client.web_probe(
                    "https://aistudio.google.com",
                    1000,
                    ["aistudio.google.com", "accounts.google.com"],
                    ["available-regions"],
                )


class GuiAutoRefreshTests(unittest.TestCase):
    def _stub(self, known_groups, live_names):
        app = ConfigApp.__new__(ConfigApp)
        app._closing = False
        app._manual_task_running = False
        app._current_node_refreshing = False
        app._known_group_names = set(known_groups)
        app._candidate_group = "组"
        app._live_candidate_names = set(live_names)
        app.group_menu = Mock()
        app.group_menu.currentText.return_value = "组"
        app.refresh_groups = Mock()
        app.refresh_candidates = Mock()
        app._set_current_node = Mock()
        app._client = Mock()
        app._background = lambda func, success=None, failure=None: success(func())
        return app

    def _client_of(self, app, proxies, snapshot):
        client = app._client.return_value
        client.proxies.return_value = proxies
        client.selector_group_snapshot.return_value = snapshot
        return client

    def test_group_set_change_triggers_group_refresh(self):
        app = self._stub({"组"}, {"节点甲"})
        self._client_of(
            app,
            {"组": {"type": "Selector"}, "新组": {"type": "Selector"}},
            ("节点甲", [{"name": "节点甲"}]),
        )
        app._refresh_current_node()
        app.refresh_groups.assert_called_once_with()
        app.refresh_candidates.assert_not_called()

    def test_member_replacement_triggers_candidate_refresh(self):
        app = self._stub({"组"}, {"节点甲"})
        self._client_of(app, {"组": {"type": "Selector"}}, ("节点乙", [{"name": "节点乙"}]))
        app._refresh_current_node()
        app.refresh_candidates.assert_called_once_with()
        app.refresh_groups.assert_not_called()

    def test_stable_subscription_only_updates_current_node(self):
        app = self._stub({"组"}, {"节点甲"})
        self._client_of(app, {"组": {"type": "Selector"}}, ("节点甲", [{"name": "节点甲"}]))
        app._refresh_current_node()
        app.refresh_groups.assert_not_called()
        app.refresh_candidates.assert_not_called()
        app._set_current_node.assert_called_once_with("节点甲")


if __name__ == "__main__":
    unittest.main()
