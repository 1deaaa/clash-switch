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
from dataclasses import asdict, dataclass, field

from mihomo_client import ControllerSettings, MihomoClient, MihomoError


ROOT = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
PID_PATH = ROOT / "monitor.pid"
LOG_PATH = ROOT / "monitor.log"
CACHE_MAX_AGE_SECONDS = 90.0


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
    controller_pipe: str = ""

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


class Monitor:
    def __init__(self, config: MonitorConfig, stop_event: threading.Event | None = None):
        self.config = config
        self.stop_event = stop_event or threading.Event()
        settings = ControllerSettings(
            url=config.controller_url,
            socket_path=config.controller_socket,
            pipe_path=config.controller_pipe,
        )
        self.client = MihomoClient(settings)
        self.failures = 0
        self.failed_node = ""
        self.round_id = 0
        self.node_cache: dict[str, dict[str, object]] = {}

    def _current(self) -> str:
        group = self.client.selector_groups().get(self.config.group)
        if not group:
            raise MihomoError(f"找不到策略组：{self.config.group}")
        return str(group.get("now") or "")

    def _update_cache(self, name: str, result: dict[str, object], round_id: int) -> None:
        cache_node_result(self.node_cache, name, result, round_id)

    def _fresh_cached_candidates(self, current: str, round_id: int) -> list[tuple[int, str, dict[str, object]]]:
        return fresh_cached_candidates(self.config, current, self.node_cache, round_id)

    def _switch_from_cache(self, current: str, round_id: int) -> dict[str, object] | None:
        switched = switch_to_best_fresh_candidate(
            self.client,
            self.config,
            current,
            self.node_cache,
            round_id,
        )
        if not switched:
            return None
        self.failures = 0
        self.failed_node = ""
        logging.info(
            "已使用%s缓存切换：%s -> %s（%d ms，缓存 %.1f 秒）",
            switched["cache_source"],
            current,
            switched["selected"],
            switched["delay"],
            switched["cache_age_seconds"],
        )
        return switched

    def check_once(self, allow_switch: bool = True) -> dict[str, object]:
        current = self._current()
        nodes = list(dict.fromkeys([*self.config.candidates, current]))
        self.round_id += 1
        round_id = self.round_id
        state: dict[str, object] = {"current_failed": False, "switched": None}

        def handle_result(name: str, result: dict[str, object]) -> None:
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
                state["switched"] = self._switch_from_cache(current, round_id)

        results = probe_nodes_delays(self.client, self.config, nodes, handle_result)
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
    with ThreadPoolExecutor(max_workers=max(1, min(256, len(tasks)))) as pool:
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
