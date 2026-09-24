from __future__ import annotations

import json
import logging
import logging.handlers
import math
import pathlib
import signal
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field, replace

from mihomo_client import ControllerSettings, MihomoClient, MihomoError


ROOT = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
PID_PATH = ROOT / "monitor.pid"
LOG_PATH = ROOT / "monitor.log"
CACHE_MAX_AGE_SECONDS = 90.0
MAX_PROBE_WORKERS = 8
AI_STUDIO_HOST = "aistudio.google.com"
NESTED_PROXY_TYPES = frozenset({"Selector", "URLTest", "Fallback", "LoadBalance"})
NON_NODE_PROXY_TYPES = frozenset(
    {
        "Selector",
        "URLTest",
        "Fallback",
        "LoadBalance",
        "Direct",
        "Reject",
        "Pass",
        "Compatible",
    }
)


@dataclass(slots=True)
class MonitorConfig:
    test_urls: list[str] = field(
        default_factory=lambda: ["https://aistudio.google.com", "https://hub.docker.com/", "https://github.com/"]
    )
    interval_seconds: int = 60
    timeout_ms: int = 5000
    failure_threshold: int = 1
    advanced_web_probe: bool = True
    allowed_redirect_hosts: list[str] = field(
        default_factory=lambda: ["aistudio.google.com", "accounts.google.com", "github.com"]
    )
    blocked_url_keywords: list[str] = field(
        default_factory=lambda: ["available-regions"]
    )
    group: str = ""
    candidates: list[str] = field(default_factory=list)
    controller_url: str = ""
    controller_socket: str = ""

    @classmethod
    def load(cls) -> "MonitorConfig":
        if not CONFIG_PATH.exists():
            return cls()
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in data.items() if key in allowed})

    def save(self) -> None:
        CONFIG_PATH.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")


def configure_logging() -> None:
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH,
        maxBytes=5 * 1024 * 1024,
        backupCount=2,
        encoding="utf-8",
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[file_handler, logging.StreamHandler()],
        force=True,
    )


def cache_node_result(
    node_cache: dict[str, dict[str, object]],
    name: str,
    result: dict[str, object],
    round_id: int,
) -> None:
    """记录节点结果及其新鲜度信息。"""
    cached = dict(result)
    cached["checked_at"] = time.monotonic()
    cached["round_id"] = round_id
    node_cache[name] = cached


def fresh_cached_candidates(
    config: MonitorConfig,
    current: str,
    node_cache: dict[str, dict[str, object]],
    round_id: int | None = None,
) -> list[tuple[int, str, dict[str, object]]]:
    """返回仍在有效期内的成功候选，并优先返回指定轮次的结果。"""
    now = time.monotonic()
    current_round: list[tuple[int, str, dict[str, object]]] = []
    previous_round: list[tuple[int, str, dict[str, object]]] = []
    for name in config.candidates:
        cached = node_cache.get(name)
        if name == current or not cached or cached.get("status") != "ok":
            continue
        try:
            checked_at = float(cached["checked_at"])
            delay = int(cached["delay"])
        except (KeyError, TypeError, ValueError):
            continue
        if now - checked_at > CACHE_MAX_AGE_SECONDS:
            continue
        item = (delay, name, cached)
        if round_id is not None and cached.get("round_id") == round_id:
            current_round.append(item)
        else:
            previous_round.append(item)
    if round_id is None:
        return previous_round
    return current_round or previous_round


def switch_to_best_fresh_candidate(
    client,
    config: MonitorConfig,
    current: str,
    node_cache: dict[str, dict[str, object]],
    round_id: int | None = None,
) -> dict[str, object] | None:
    """使用最新的成功缓存切换到当前可选范围内延迟最低的节点。"""
    available = fresh_cached_candidates(config, current, node_cache, round_id)
    if not available:
        return None
    delay, best, cached = min(available)
    client.select(config.group, best)
    if round_id is None:
        source = "最近一次"
    elif cached.get("round_id") == round_id:
        source = "本轮"
    else:
        source = "上一轮"
    age = time.monotonic() - float(cached["checked_at"])
    return {
        "selected": best,
        "delay": delay,
        "cache_source": source,
        "cache_age_seconds": age,
    }


def switch_to_best_verified_candidate(
    client,
    config: MonitorConfig,
    current: str,
    node_cache: dict[str, dict[str, object]],
    round_id: int | None = None,
) -> dict[str, object] | None:
    """按缓存延迟尝试候选，并在真正切换后复核 AI Studio。"""
    rejected: dict[str, str] = {}
    selected_any = False
    switched: dict[str, object] | None = None
    try:
        while True:
            available = [
                item
                for item in fresh_cached_candidates(config, current, node_cache, round_id)
                if item[1] not in rejected
            ]
            if not available:
                return None
            delay, best, cached = min(available)
            try:
                client.select(config.group, best)
                selected_any = True
            except MihomoError as exc:
                rejected[best] = f"切换失败：{exc}"
                cache_node_result(
                    node_cache,
                    best,
                    {"status": "failed", "error": rejected[best]},
                    round_id or 0,
                )
                continue

            page_error = _ai_studio_page_error(client, config)
            if page_error:
                rejected[best] = page_error
                cache_node_result(
                    node_cache,
                    best,
                    {"status": "failed", "error": page_error},
                    round_id or 0,
                )
                logging.warning("手动切换候选复核失败：%s（%s），继续尝试其它节点", best, page_error)
                continue

            source = "最近一次" if round_id is None else (
                "本轮" if cached.get("round_id") == round_id else "上一轮"
            )
            age = time.monotonic() - float(cached["checked_at"])
            switched = {
                "selected": best,
                "delay": delay,
                "cache_source": source,
                "cache_age_seconds": age,
            }
            return switched
    finally:
        if selected_any and switched is None:
            try:
                client.select(config.group, current)
            except MihomoError as exc:
                logging.error("手动候选复核均失败，恢复原节点失败：%s", exc)


def _ai_studio_test_url(config: MonitorConfig) -> str | None:
    if not config.advanced_web_probe:
        return None
    for url in config.test_urls:
        if (urllib.parse.urlparse(url).hostname or "").lower() == AI_STUDIO_HOST:
            return url
    return None


def _ai_studio_page_error(client, config: MonitorConfig) -> str | None:
    url = _ai_studio_test_url(config)
    if not url:
        return None
    try:
        client.web_probe(
            url,
            config.timeout_ms,
            config.allowed_redirect_hosts,
            config.blocked_url_keywords,
        )
    except Exception as exc:
        return f"AI Studio 网页复核失败：{exc}"
    return None


def resolve_runtime_group(
    config: MonitorConfig,
    groups: dict[str, dict[str, object]],
    proxies: dict[str, dict[str, object]] | None = None,
    prefer_largest: bool = False,
) -> str:
    if config.group in groups and not prefer_largest:
        return config.group

    def score(name: str) -> int:
        lowered = name.casefold()
        for keyword, value in (("aistudio", 4), ("gemini", 3), ("google", 2), ("ai", 1)):
            if keyword in lowered:
                return value
        return 0

    def real_names(name: str) -> set[str]:
        """递归统计策略组中的真实节点，避免只按直接成员误判大小。"""
        source = proxies or groups
        names: set[str] = set()
        visited: set[str] = set()

        def visit(group_name: str) -> None:
            if group_name in visited:
                return
            visited.add(group_name)
            for raw_item in source.get(group_name, {}).get("all", []):
                item_name = str(raw_item)
                item = source.get(item_name, {})
                item_type = str(item.get("type") or "")
                if item_type in NESTED_PROXY_TYPES:
                    visit(item_name)
                elif item_type not in NON_NODE_PROXY_TYPES:
                    names.add(item_name)

        visit(name)
        return names

    def real_count(name: str) -> int:
        return len(real_names(name))

    if not groups:
        raise MihomoError(f"找不到策略组：{config.group}；当前 Selector 策略组：无")
    ranked = sorted(
        ((score(name), real_count(name), name) for name in groups),
        key=lambda item: (item[0], item[1], item[2]),
        reverse=True,
    )
    if prefer_largest or config.group not in groups:
        candidates = [item for item in ranked if item[1] > 0] or ranked
        if candidates:
            return max(candidates, key=lambda item: (item[1], item[0], item[2]))[2]
    if _ai_studio_test_url(config):
        named = [item for item in ranked if item[0] > 0]
        if named:
            return named[0][2]
    return ranked[0][2]


def resolve_route_group(
    client,
    config: MonitorConfig,
    groups: dict[str, dict[str, object]],
    proxies: dict[str, dict[str, object]],
) -> str | None:
    """优先使用 AI Studio 登录接口实际命中的策略组。"""
    if not _ai_studio_test_url(config) or not hasattr(client, "selector_group_for_host"):
        return None
    # 页面和登录后的 RPC 可能命中不同规则；RPC 决定模型列表能否真正加载。
    for host in ("alkalimakersuite-pa.clients6.google.com", "aistudio.google.com"):
        try:
            group = client.selector_group_for_host(host, proxies=proxies)
        except Exception as exc:
            logging.debug("读取 %s 的路由规则失败：%s", host, exc)
            continue
        if group in groups:
            return group
    return None


def _resolve_live_candidates(
    configured: list[str],
    live_candidates: list[dict[str, str]],
) -> tuple[list[str], bool]:
    available = list(dict.fromkeys(item["name"] for item in live_candidates))
    available_set = set(available)
    configured_names = list(dict.fromkeys(configured))
    selected = [name for name in configured_names if name in available_set]
    if selected and len(selected) == len(configured_names):
        return selected, False
    return available, bool(available)


class Monitor:
    def __init__(self, config: MonitorConfig, stop_event: threading.Event | None = None):
        self.config = config
        self.stop_event = stop_event or threading.Event()
        settings = ControllerSettings(url=config.controller_url, socket_path=config.controller_socket)
        self.client = MihomoClient(settings)
        self.failures = 0
        self.failed_node = ""
        self.round_id = 0
        self.node_cache: dict[str, dict[str, object]] = {}
        self._last_runtime_group = ""
        self._last_candidate_fallback: tuple[str, tuple[str, ...]] | None = None
        self._last_live_signature: tuple[str, tuple[tuple[str, str], ...]] | None = None

    def _reload_config(self) -> None:
        try:
            latest = MonitorConfig.load()
        except Exception:
            logging.exception("重新读取监控配置失败，继续使用上一份有效配置")
            return
        if latest == self.config:
            return

        previous = self.config
        probe_settings_changed = any(
            getattr(previous, field_name) != getattr(latest, field_name)
            for field_name in (
                "group",
                "candidates",
                "test_urls",
                "timeout_ms",
                "advanced_web_probe",
                "allowed_redirect_hosts",
                "blocked_url_keywords",
            )
        )
        controller_changed = any(
            getattr(previous, field_name) != getattr(latest, field_name)
            for field_name in ("controller_url", "controller_socket")
        )
        self.config = latest
        if probe_settings_changed:
            self.node_cache.clear()
            self.failures = 0
            self.failed_node = ""
        if controller_changed:
            settings = ControllerSettings(url=latest.controller_url, socket_path=latest.controller_socket)
            self.client = MihomoClient(settings)
        logging.info("监控配置已重新加载")

    def _update_cache(self, name: str, result: dict[str, object], round_id: int) -> None:
        cache_node_result(self.node_cache, name, result, round_id)

    def _switch_from_cache(
        self,
        current: str,
        round_id: int,
        config: MonitorConfig,
        rejected_candidates: dict[str, str],
        restore_proxy: str = "",
    ) -> dict[str, object] | None:
        selected_any = False
        switched: dict[str, object] | None = None
        try:
            while True:
                available = [
                    item
                    for item in fresh_cached_candidates(config, current, self.node_cache, round_id)
                    if item[1] not in rejected_candidates
                ]
                if not available:
                    return None

                delay, best, cached = min(available)
                try:
                    self.client.select(config.group, best)
                    selected_any = True
                except MihomoError as exc:
                    error = f"切换候选节点失败：{exc}"
                    rejected_candidates[best] = error
                    self._update_cache(best, {"status": "failed", "error": error}, round_id)
                    logging.warning("候选节点无法切换：%s（%s），继续检查其它候选", best, exc)
                    continue

                page_error = _ai_studio_page_error(self.client, config)
                if page_error:
                    rejected_candidates[best] = page_error
                    self._update_cache(best, {"status": "failed", "error": page_error}, round_id)
                    logging.warning("候选节点 AI Studio 网页复核失败：%s（%s），继续检查其它候选", best, page_error)
                    continue

                if round_id is None:
                    source = "最近一次"
                elif cached.get("round_id") == round_id:
                    source = "本轮"
                else:
                    source = "上一轮"
                age = time.monotonic() - float(cached["checked_at"])
                self.failures = 0
                self.failed_node = ""
                switched = {
                    "selected": best,
                    "delay": delay,
                    "cache_source": source,
                    "cache_age_seconds": age,
                }
                logging.info(
                    "已使用%s缓存切换：%s -> %s（%d ms，缓存 %.1f 秒）",
                    source,
                    current,
                    best,
                    delay,
                    age,
                )
                return switched
        finally:
            if selected_any and switched is None and restore_proxy and restore_proxy not in rejected_candidates:
                try:
                    self.client.select(config.group, restore_proxy)
                    logging.info("候选网页复核均失败，已恢复原节点：%s", restore_proxy)
                except MihomoError as exc:
                    logging.error("候选网页复核均失败，恢复原节点也失败：%s", exc)

    def check_once(self, allow_switch: bool = True) -> dict[str, object]:
        if hasattr(self.client, "proxies"):
            proxies = self.client.proxies()
            groups = {name: item for name, item in proxies.items() if item.get("type") == "Selector"}
        else:
            groups = self.client.selector_groups()
            proxies = groups
        route_group = resolve_route_group(self.client, self.config, groups, proxies)
        prefer_largest = False
        if route_group:
            runtime_group = route_group
        else:
            if self.config.group in groups and self.config.candidates and hasattr(self.client, "proxies"):
                _, configured_live = self.client.selector_group_candidates(self.config.group, proxies=proxies)
                live_names = {item["name"] for item in configured_live}
                prefer_largest = not set(self.config.candidates).issubset(live_names)
            runtime_group = resolve_runtime_group(self.config, groups, proxies, prefer_largest=prefer_largest)
        restore_proxy = str(groups[runtime_group].get("now") or "")
        if hasattr(self.client, "proxies"):
            current, live_candidates = self.client.selector_group_snapshot(runtime_group, proxies=proxies)
        else:
            current, live_candidates = self.client.selector_group_snapshot(runtime_group)
        if not current:
            raise MihomoError(f"策略组没有当前节点：{runtime_group}")
        live_signature = (
            runtime_group,
            tuple(sorted((str(item["name"]), str(item.get("provider") or "")) for item in live_candidates)),
        )
        if live_signature != self._last_live_signature:
            if self._last_live_signature is not None:
                logging.info("检测到策略组或订阅节点列表变化，清除旧探测缓存")
            self.node_cache.clear()
            self.failures = 0
            self.failed_node = ""
            self._last_live_signature = live_signature
        if runtime_group != self.config.group and runtime_group != self._last_runtime_group:
            logging.warning(
                "配置策略组 %s 未命中 AI Studio 实际路由，自动使用策略组 %s",
                self.config.group,
                runtime_group,
            )
        self._last_runtime_group = runtime_group

        candidates, used_fallback = _resolve_live_candidates(self.config.candidates, live_candidates)
        if used_fallback:
            signature = (runtime_group, tuple(candidates))
            if signature != self._last_candidate_fallback:
                logging.warning(
                    "保存的候选节点与当前订阅不匹配，改用策略组 %s 的全部 %d 个真实节点",
                    runtime_group,
                    len(candidates),
                )
            self._last_candidate_fallback = signature
        else:
            self._last_candidate_fallback = None

        round_config = replace(self.config, group=runtime_group, candidates=candidates)
        nodes = list(dict.fromkeys([current, *candidates]))
        self.round_id += 1
        round_id = self.round_id
        state: dict[str, object] = {
            "current_failed": False,
            "switched": None,
            "rejected_candidates": {},
        }

        def handle_result(name: str, result: dict[str, object]) -> None:
            rejected = state["rejected_candidates"]
            if name in rejected:
                result["status"] = "failed"
                result["error"] = rejected[name]
            elif name == current and result["status"] == "ok":
                page_error = _ai_studio_page_error(self.client, round_config)
                if page_error:
                    result["status"] = "failed"
                    result["error"] = page_error
            self._update_cache(name, result, round_id)
            _log_node_result(name, result)
            if name == current:
                if result["status"] == "ok":
                    self.failures = 0
                    self.failed_node = ""
                    return
                if self.failed_node != current:
                    self.failures = 0
                    self.failed_node = current
                self.failures += 1
                state["current_failed"] = True
                logging.warning(
                    "当前节点探测失败（%d/%d）：%s（%s）",
                    self.failures,
                    self.config.failure_threshold,
                    current,
                    result["error"],
                )
            if (
                allow_switch
                and state["current_failed"]
                and state["switched"] is None
                and self.failures >= self.config.failure_threshold
            ):
                state["switched"] = self._switch_from_cache(
                    current,
                    round_id,
                    round_config,
                    rejected,
                    restore_proxy=restore_proxy,
                )

        results = probe_nodes_delays(self.client, round_config, nodes, handle_result)
        current_result = results[current]
        if current_result["status"] == "ok":
            logging.info("本轮完成：当前节点 %s 可用，保持不变", current)
            return {
                "status": "ok",
                "current": current,
                "delay": current_result["delay"],
                "delays": current_result["delays"],
                "nodes": results,
            }

        switched = state["switched"]
        if switched:
            logging.info("本轮完成：当前节点 %s 失败，已切换到 %s", current, switched["selected"])
            return {
                "status": "switched",
                "current": current,
                "switched": True,
                "nodes": results,
                **switched,
            }
        if self.failures < self.config.failure_threshold or not allow_switch:
            return {"status": "failed", "current": current, "switched": False, "nodes": results}
        logging.error("缓存和本轮结果中都没有可切换的成功候选，保持 %s", current)
        return {"status": "no_candidate", "current": current, "switched": False, "nodes": results}

    def run(self) -> None:
        PID_PATH.write_text(str(__import__("os").getpid()), encoding="ascii")
        logging.info("监控服务启动：组=%s，网址=%s，间隔=%d 秒", self.config.group, self.config.test_urls, self.config.interval_seconds)
        try:
            while not self.stop_event.is_set():
                started = time.monotonic()
                try:
                    self._reload_config()
                    self.check_once()
                except Exception:
                    logging.exception("本轮监控发生错误")
                elapsed = time.monotonic() - started
                self.stop_event.wait(_seconds_until_next_round(elapsed, self.config.interval_seconds))
        finally:
            PID_PATH.unlink(missing_ok=True)
            logging.info("监控服务已停止")


def _delay_probe_target(url: str, strict: bool) -> tuple[str, str]:
    """生成可由 Mihomo 独立节点探测的目标与期望状态码。"""
    if not strict:
        return url, ""
    parsed = urllib.parse.urlparse(url)
    if (parsed.hostname or "").lower() == "aistudio.google.com" and parsed.path in {"", "/"}:
        parsed = parsed._replace(path="/welcome")
        return urllib.parse.urlunparse(parsed), "200-299"
    return url, "200-399"


def _seconds_until_next_round(elapsed: float, interval_seconds: int) -> float:
    """按轮次起点保持固定节拍，并跳过已经错过的时间点。"""
    interval = max(5, interval_seconds)
    periods = max(1, math.ceil(elapsed / interval))
    return max(0.0, periods * interval - elapsed)


def _probe_worker_count(task_count: int) -> int:
    """限制探测线程数量，避免大量线程栈长期占用虚拟地址空间。"""
    return max(1, min(MAX_PROBE_WORKERS, task_count))


def probe_nodes_delays(client, config: MonitorConfig, nodes, on_result=None) -> dict[str, dict[str, object]]:
    """在同一线程池中并行测试全部节点与全部目标网址。"""
    names = list(dict.fromkeys(str(node) for node in nodes))
    results = {name: {"status": "ok", "delays": {}} for name in names}
    tasks = []
    for name in names:
        for original_url in config.test_urls:
            target_url, expected = _delay_probe_target(original_url, config.advanced_web_probe)
            tasks.append((name, original_url, target_url, expected))

    def probe(task):
        name, original_url, target_url, expected = task
        delay = client.test_delay(name, target_url, config.timeout_ms, expected)
        return name, original_url, delay

    remaining = {name: len(config.test_urls) for name in names}
    with ThreadPoolExecutor(max_workers=_probe_worker_count(len(tasks))) as pool:
        futures = {pool.submit(probe, task): task for task in tasks}
        for future in as_completed(futures):
            task = futures[future]
            name, original_url, _, _ = task
            try:
                _, _, delay = future.result()
                results[name]["delays"][original_url] = delay
            except Exception as exc:
                results[name]["status"] = "failed"
                results[name].setdefault("error", str(exc))
            remaining[name] -= 1
            if remaining[name] == 0:
                item = results[name]
                if item["status"] == "ok":
                    item["delay"] = max(item["delays"].values())
                if on_result:
                    on_result(name, item)
    return results


def probe_node_delays(client, config: MonitorConfig, node: str) -> tuple[int, dict[str, int]]:
    """测试一个节点对全部目标网址的 Mihomo 延迟。"""
    result = probe_nodes_delays(client, config, [node])[node]
    if result["status"] != "ok":
        raise MihomoError(str(result["error"]))
    return int(result["delay"]), dict(result["delays"])


def _log_node_result(name: str, result: dict[str, object]) -> None:
    if result["status"] == "ok":
        logging.info("节点可用：%s，最大延迟 %d ms，明细=%s", name, result["delay"], result["delays"])
    else:
        logging.warning("节点不可用：%s（%s）", name, result["error"])


def probe_selected_web(client, config: MonitorConfig) -> dict[str, dict[str, object]]:
    """并发检查当前已选节点对全部目标网址的网页落点。"""
    def probe(url: str) -> tuple[str, dict[str, object]]:
        page = client.web_probe(
            url,
            config.timeout_ms,
            config.allowed_redirect_hosts,
            config.blocked_url_keywords,
        )
        return url, page

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(config.test_urls)))) as pool:
        return dict(pool.map(probe, config.test_urls))


def main() -> None:
    configure_logging()
    stop_event = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop_event.set())
    Monitor(MonitorConfig.load(), stop_event).run()


if __name__ == "__main__":
    main()
