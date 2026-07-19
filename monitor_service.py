from __future__ import annotations

import json
import logging
import pathlib
import signal
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field

from mihomo_client import ControllerSettings, MihomoClient, MihomoError


ROOT = pathlib.Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
PID_PATH = ROOT / "monitor.pid"
LOG_PATH = ROOT / "monitor.log"


@dataclass(slots=True)
class MonitorConfig:
    test_urls: list[str] = field(
        default_factory=lambda: ["https://aistudio.google.com", "https://hub.docker.com/", "https://github.com/"]
    )
    interval_seconds: int = 60
    timeout_ms: int = 10000
    failure_threshold: int = 2
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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
        force=True,
    )


class Monitor:
    def __init__(self, config: MonitorConfig, stop_event: threading.Event | None = None):
        self.config = config
        self.stop_event = stop_event or threading.Event()
        settings = ControllerSettings(url=config.controller_url, socket_path=config.controller_socket)
        self.client = MihomoClient(settings)
        self.failures = 0

    def _current(self) -> str:
        group = self.client.selector_groups().get(self.config.group)
        if not group:
            raise MihomoError(f"找不到策略组：{self.config.group}")
        return str(group.get("now") or "")

    def _web_probe(self) -> dict[str, object] | None:
        if not self.config.advanced_web_probe:
            return None
        pages = probe_selected_web(self.client, self.config)
        return {"targets": pages}

    def check_once(self, allow_switch: bool = True) -> dict[str, object]:
        current = self._current()
        try:
            delay, delays = probe_node_delays(self.client, self.config, current)
            page = self._web_probe()
            self.failures = 0
            if page:
                landing = {url: item["final_url"] for url, item in page["targets"].items()}
                logging.info("当前节点可用：%s，最大延迟 %d ms，网页落点 %s", current, delay, landing)
            else:
                logging.info("当前节点可用：%s，延迟 %d ms", current, delay)
            return {"status": "ok", "current": current, "delay": delay, "delays": delays, "page": page}
        except Exception as exc:
            self.failures += 1
            logging.warning("当前节点探测失败（%d/%d）：%s", self.failures, self.config.failure_threshold, exc)
        if self.failures < self.config.failure_threshold or not allow_switch:
            return {"status": "failed", "current": current, "switched": False}

        results: list[tuple[int, str]] = []
        for candidate in self.config.candidates:
            if candidate == current:
                continue
            try:
                delay, _ = probe_node_delays(self.client, self.config, candidate)
                page = None
                if self.config.advanced_web_probe:
                    self.client.select(self.config.group, candidate)
                    page = self._web_probe()
                results.append((delay, candidate))
                if page:
                    landing = {url: item["final_url"] for url, item in page["targets"].items()}
                    logging.info("候选节点可用：%s，最大延迟 %d ms，网页落点 %s", candidate, delay, landing)
                else:
                    logging.info("候选节点可用：%s，延迟 %d ms", candidate, delay)
            except Exception as exc:
                logging.warning("候选节点不可用：%s（%s）", candidate, exc)
        if not results:
            self.client.select(self.config.group, current)
            logging.error("没有可切换的可用候选节点，已恢复 %s", current)
            return {"status": "no_candidate", "current": current, "switched": False}
        delay, best = min(results)
        self.client.select(self.config.group, best)
        self.failures = 0
        logging.info("已切换：%s -> %s（%d ms）", current, best, delay)
        return {"status": "switched", "current": current, "selected": best, "delay": delay, "switched": True}

    def run(self) -> None:
        PID_PATH.write_text(str(__import__("os").getpid()), encoding="ascii")
        logging.info("监控服务启动：组=%s，网址=%s，间隔=%d 秒", self.config.group, self.config.test_urls, self.config.interval_seconds)
        try:
            while not self.stop_event.is_set():
                try:
                    self.check_once()
                except Exception:
                    logging.exception("本轮监控发生错误")
                self.stop_event.wait(max(5, self.config.interval_seconds))
        finally:
            PID_PATH.unlink(missing_ok=True)
            logging.info("监控服务已停止")


def probe_node_delays(client, config: MonitorConfig, node: str) -> tuple[int, dict[str, int]]:
    """并发测试一个节点对全部目标网址的 Mihomo 延迟。"""
    def probe(url: str) -> tuple[str, int]:
        return url, client.test_delay(node, url, config.timeout_ms)

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(config.test_urls)))) as pool:
        delays = dict(pool.map(probe, config.test_urls))
    return max(delays.values()), delays


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
