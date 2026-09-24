from __future__ import annotations

import json
import ctypes
import hashlib
import http.client
import logging
import os
import pathlib
import re
import socket
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import yaml

from browser_probe import BrowserProbe, BrowserProbeError, BrowserProbeUnavailable, find_storage_state


DEFAULT_WINDOWS_PIPE_PATH = r"\\.\pipe\verge-mihomo"
WINDOWS_PIPE_MAX_CONCURRENCY = 16
WINDOWS_PIPE_OPEN_RETRIES = 3
WINDOWS_PIPE_RETRY_DELAY_SECONDS = 0.1
_WINDOWS_PIPE_SEMAPHORE = threading.BoundedSemaphore(WINDOWS_PIPE_MAX_CONCURRENCY)


class MihomoError(RuntimeError):
    """Mihomo 控制接口调用失败。"""


class MihomoTransportError(MihomoError):
    """Mihomo 控制连接暂时不可用，可安全重试请求。"""


_NESTED_PROXY_TYPES = frozenset({"Selector", "URLTest", "Fallback", "LoadBalance"})
_NON_NODE_PROXY_TYPES = frozenset(
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
_BROWSER_PROBE_LOCK = threading.RLock()
_BROWSER_PROBE_CALL_LOCK = threading.Lock()
_SHARED_BROWSER_PROBE: BrowserProbe | bool | None = None
_BROWSER_PROBE_UNAVAILABLE_AT = 0.0
_BROWSER_PROBE_RETRY_SECONDS = 30.0


def _read_response_body(response, limit: int) -> str:
    """读取网页正文；连接提前关闭时保留已经收到的内容。"""
    remaining = max(0, int(limit))
    chunks: list[bytes] = []
    while remaining:
        try:
            chunk = response.read(min(64 * 1024, remaining))
        except http.client.IncompleteRead as exc:
            chunk = exc.partial or b""
            if chunk:
                chunks.append(chunk[:remaining])
            break
        except OSError:
            if not chunks:
                raise
            break
        if not chunk:
            break
        chunks.append(chunk[:remaining])
        remaining -= len(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


@dataclass(slots=True)
class ControllerSettings:
    url: str = ""
    secret: str = ""
    socket_path: str = ""
    pipe_path: str = ""


def _verge_config_candidates() -> list[pathlib.Path]:
    home = pathlib.Path.home()
    if os.name == "nt":
        appdata = pathlib.Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
        return [appdata / "io.github.clash-verge-rev.clash-verge-rev/config.yaml"]
    return [
        home / ".local/share/io.github.clash-verge-rev.clash-verge-rev/config.yaml",
        home / ".config/clash-verge-rev/config.yaml",
    ]


def _is_socket(path: pathlib.Path) -> bool:
    try:
        return stat.S_ISSOCK(path.stat().st_mode)
    except OSError:
        return False


def _runtime_socket_candidates() -> list[pathlib.Path]:
    """返回 Clash Verge Service 当前可能创建的 Unix Socket 路径。"""
    if os.name == "nt":
        return []
    uid = str(os.getuid())
    candidates = [
        pathlib.Path(f"/run/clash-verge-service/users/{uid}/verge-mihomo.sock"),
        pathlib.Path(f"/run/user/{uid}/clash-verge-service/users/{uid}/verge-mihomo.sock"),
        pathlib.Path("/tmp/verge/verge-mihomo.sock"),
        pathlib.Path("/tmp/clash-verge-rev/verge-mihomo.sock"),
    ]
    service_users = pathlib.Path("/run/clash-verge-service/users")
    if service_users.is_dir():
        candidates.extend(service_users.glob("*/verge-mihomo.sock"))
    return list(dict.fromkeys(candidates))


def discover_settings(overrides: ControllerSettings | None = None) -> ControllerSettings:
    settings = overrides or ControllerSettings()
    secret = settings.secret
    url = settings.url
    socket_path = settings.socket_path
    pipe_path = settings.pipe_path
    for path in _verge_config_candidates():
        if not path.exists():
            continue
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        secret = secret or str(data.get("secret") or "")
        url = url or str(data.get("external-controller") or "")
        socket_path = socket_path or str(data.get("external-controller-unix") or "")
        pipe_path = pipe_path or str(data.get("external-controller-pipe") or "")
        break
    if os.name != "nt":
        configured_socket = pathlib.Path(socket_path).expanduser() if socket_path else None
        if configured_socket is None or not _is_socket(configured_socket):
            socket_path = next(
                (str(candidate) for candidate in _runtime_socket_candidates() if _is_socket(candidate)),
                "",
            )
    if url and "://" not in url:
        url = f"http://{url}"
    if os.name == "nt" and not pipe_path:
        pipe_path = DEFAULT_WINDOWS_PIPE_PATH
    return ControllerSettings(url=url, secret=secret, socket_path=socket_path, pipe_path=pipe_path)


def _decode_http(raw: bytes) -> tuple[int, bytes]:
    try:
        head, body = raw.split(b"\r\n\r\n", 1)
        status = int(head.split(b"\r\n", 1)[0].split()[1])
    except (ValueError, IndexError) as exc:
        raise MihomoError("控制器返回了无效的 HTTP 响应") from exc
    headers: dict[bytes, bytes] = {}
    for line in head.split(b"\r\n")[1:]:
        if b":" in line:
            key, value = line.split(b":", 1)
            headers[key.lower()] = value.strip().lower()
    if headers.get(b"transfer-encoding") == b"chunked":
        decoded = bytearray()
        while body:
            size_line, body = body.split(b"\r\n", 1)
            size = int(size_line.split(b";", 1)[0], 16)
            if size == 0:
                break
            decoded.extend(body[:size])
            body = body[size + 2 :]
        body = bytes(decoded)
    return status, body


class MihomoClient:
    def __init__(self, settings: ControllerSettings | None = None, timeout: float = 10.0):
        self.settings = discover_settings(settings)
        self.timeout = timeout
        self._browser_probe: BrowserProbe | bool | None = None
        self._aistudio_api_keys: tuple[str, ...] = ()

    @staticmethod
    def _windows_pipe_error(error_code: int, path: str) -> OSError:
        message = ctypes.FormatError(error_code).strip() or "Windows 命名管道操作失败"
        return OSError(error_code, message, path)

    @classmethod
    def _open_windows_pipe(cls, path: str, timeout: float):
        """使用 Win32 API 打开命名管道，避免并行探测触发 CRT 错误。"""
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WaitNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        kernel32.WaitNamedPipeW.restype = ctypes.c_int
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int

        generic_read = 0x80000000
        generic_write = 0x40000000
        open_existing = 3
        invalid_handle = ctypes.c_void_p(-1).value
        retryable_errors = {2, 121, 231, 232}
        deadline = time.monotonic() + max(0.1, timeout)
        last_error = 2
        for attempt in range(WINDOWS_PIPE_OPEN_RETRIES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wait_ms = max(1, min(1000, round(remaining * 1000)))
            if not kernel32.WaitNamedPipeW(path, wait_ms):
                last_error = ctypes.get_last_error()
            else:
                handle = kernel32.CreateFileW(
                    path,
                    generic_read | generic_write,
                    0,
                    None,
                    open_existing,
                    0,
                    None,
                )
                if handle != invalid_handle:
                    try:
                        descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
                    except OSError:
                        kernel32.CloseHandle(handle)
                        raise
                    return os.fdopen(descriptor, "r+b", buffering=0)
                last_error = ctypes.get_last_error()
            if last_error not in retryable_errors:
                break
            if attempt + 1 < WINDOWS_PIPE_OPEN_RETRIES:
                time.sleep(min(
                    WINDOWS_PIPE_RETRY_DELAY_SECONDS * (attempt + 1),
                    max(0.0, deadline - time.monotonic()),
                ))
        raise cls._windows_pipe_error(last_error, path)

    def _windows_pipe_request(self, raw_request: bytes) -> tuple[int, bytes]:
        """限制本进程管道并发，并在管道暂时繁忙时重试打开。"""
        if not self.settings.pipe_path:
            raise MihomoTransportError("未配置 Clash Verge Rev 命名管道")
        acquired = _WINDOWS_PIPE_SEMAPHORE.acquire(timeout=max(0.1, self.timeout))
        if not acquired:
            raise MihomoTransportError("等待 Clash Verge Rev 命名管道并发槽位超时")
        try:
            try:
                with self._open_windows_pipe(self.settings.pipe_path, self.timeout) as pipe:
                    pipe.write(raw_request)
                    pipe.flush()
                    return _decode_http(pipe.read())
            except OSError as exc:
                raise MihomoTransportError(f"无法连接 Clash Verge Rev 命名管道：{exc}") from exc
        finally:
            _WINDOWS_PIPE_SEMAPHORE.release()

    def _raw_request(self, method: str, path: str, payload: bytes | None) -> tuple[int, bytes]:
        headers = ["Host: localhost", "Connection: close", "Accept: application/json"]
        if self.settings.secret:
            headers.append(f"Authorization: Bearer {self.settings.secret}")
        if payload is not None:
            headers.extend(["Content-Type: application/json", f"Content-Length: {len(payload)}"])
        request = f"{method} {path} HTTP/1.1\r\n" + "\r\n".join(headers) + "\r\n\r\n"
        raw_request = request.encode("utf-8") + (payload or b"")
        if os.name == "nt":
            return self._windows_pipe_request(raw_request)
        if self.settings.socket_path:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(self.timeout)
                    sock.connect(self.settings.socket_path)
                    sock.sendall(raw_request)
                    chunks = []
                    while chunk := sock.recv(65536):
                        chunks.append(chunk)
                return _decode_http(b"".join(chunks))
            except OSError as exc:
                raise MihomoTransportError(f"无法连接 Mihomo Unix Socket：{exc}") from exc
        raise MihomoTransportError("未发现可用的 Mihomo 控制器")

    def _request_once(self, method: str, path: str, payload: bytes | None) -> Any:
        """执行一次请求；本地 Socket 优先，避免使用已经失效的 TCP 控制地址。"""
        if self.settings.socket_path:
            try:
                status, body = self._raw_request(method, path, payload)
            except MihomoError:
                if not self.settings.url:
                    raise
                status, body = self._url_request(method, path, payload)
        elif self.settings.url:
            status, body = self._url_request(method, path, payload)
        else:
            status, body = self._raw_request(method, path, payload)
        if status < 200 or status >= 300:
            message = body.decode("utf-8", errors="replace")
            raise MihomoError(f"控制器返回 HTTP {status}：{message}")
        if not body.strip():
            return None
        return json.loads(body)

    def _url_request(self, method: str, path: str, payload: bytes | None) -> tuple[int, bytes]:
        if not self.settings.url:
            raise MihomoError("未配置 Mihomo HTTP 控制器")
        url = self.settings.url.rstrip("/") + path
        headers = {"Accept": "application/json"}
        if self.settings.secret:
            headers["Authorization"] = f"Bearer {self.settings.secret}"
        request = urllib.request.Request(url, data=payload, method=method, headers=headers)
        if payload is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except Exception as exc:
            raise MihomoTransportError(f"控制器请求失败：{exc}") from exc

    def request(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
        last_error: MihomoError | None = None
        for attempt in range(2):
            try:
                return self._request_once(method, path, payload)
            except MihomoError as exc:
                last_error = exc
                if attempt or not isinstance(exc, MihomoTransportError):
                    break
                refreshed = discover_settings(self.settings)
                self.settings = refreshed
                # Clash 重载时可能复用同一个 Socket 路径；即使路径没变也短暂重试一次。
                time.sleep(0.15)
        raise last_error or MihomoError("控制器请求失败")

    def version(self) -> str:
        return str(self.request("GET", "/version").get("version", "未知"))

    def proxies(self) -> dict[str, dict[str, Any]]:
        return self.request("GET", "/proxies").get("proxies", {})

    def selector_groups(self) -> dict[str, dict[str, Any]]:
        return {name: item for name, item in self.proxies().items() if item.get("type") == "Selector"}

    def current_selector(self, group: str) -> str:
        group_data = self.selector_groups().get(group)
        if group_data is None:
            raise MihomoError(f"找不到 Selector 策略组：{group}")
        return str(group_data.get("now") or "")

    def selector_group_candidates(
        self,
        group: str,
        proxies: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[str, list[dict[str, str]]]:
        """读取策略组当前节点及其可选的真实节点。"""
        return self._selector_group_data(group, resolve_current=False, proxies=proxies)

    def selector_group_snapshot(
        self,
        group: str,
        proxies: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[str, list[dict[str, str]]]:
        """读取策略组最终生效的节点及其可选的真实节点。"""
        return self._selector_group_data(group, resolve_current=True, proxies=proxies)

    def _selector_group_data(
        self,
        group: str,
        resolve_current: bool,
        proxies: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[str, list[dict[str, str]]]:
        proxies = proxies if proxies is not None else self.proxies()
        group_data = proxies.get(group, {})
        if group_data.get("type") != "Selector":
            raise MihomoError(f"找不到 Selector 策略组：{group}")

        current = str(group_data.get("now") or "")
        if resolve_current:
            visited = {group}
            while current and current not in visited:
                visited.add(current)
                item = proxies.get(current, {})
                if item.get("type") not in {"Selector", "URLTest", "Fallback", "LoadBalance"}:
                    break
                nested = str(item.get("now") or "")
                if not nested:
                    break
                current = nested

        candidates = self._selector_leaf_candidates(proxies, group)
        return current, candidates

    @staticmethod
    def _selector_leaf_candidates(
        proxies: dict[str, dict[str, Any]],
        group: str,
    ) -> list[dict[str, str]]:
        """展开嵌套策略组，返回去重后的真实节点。"""
        candidates: list[dict[str, str]] = []
        visited_groups: set[str] = set()
        seen_nodes: set[str] = set()

        def visit(group_name: str) -> None:
            if group_name in visited_groups:
                return
            visited_groups.add(group_name)
            group_data = proxies.get(group_name, {})
            for raw_name in group_data.get("all", []):
                name = str(raw_name)
                item = proxies.get(name, {})
                item_type = str(item.get("type") or "")
                if item_type in _NESTED_PROXY_TYPES:
                    visit(name)
                    continue
                if item_type in _NON_NODE_PROXY_TYPES or name in seen_nodes:
                    continue
                seen_nodes.add(name)
                candidates.append(
                    {"name": name, "provider": str(item.get("provider-name") or "本地配置")}
                )

        visit(group)
        return candidates

    def test_delay(self, proxy: str, url: str, timeout_ms: int, expected: str = "") -> int:
        params: dict[str, object] = {"url": url, "timeout": timeout_ms}
        if expected:
            params["expected"] = expected
        query = urllib.parse.urlencode(params)
        path = f"/proxies/{urllib.parse.quote(proxy, safe='')}/delay?{query}"
        return int(self.request("GET", path)["delay"])

    def select(self, group: str, proxy: str) -> None:
        path = f"/proxies/{urllib.parse.quote(group, safe='')}"
        self.request("PUT", path, {"name": proxy})

    def select_node(
        self,
        group: str,
        proxy: str,
        proxies: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """切换真实叶节点，必要时沿嵌套 Selector 找到可写的下级组。"""
        proxies = proxies if proxies is not None else self.proxies()
        visited: set[str] = set()

        def select_from(selector: str) -> bool:
            if selector in visited:
                return False
            visited.add(selector)
            data = proxies.get(selector, {})
            members = [str(item) for item in data.get("all", [])]
            if proxy in members:
                self.select(selector, proxy)
                return True
            for member in members:
                if proxies.get(member, {}).get("type") == "Selector" and select_from(member):
                    return True
            return False

        if not select_from(group):
            raise MihomoError(f"策略组 {group} 中不存在节点：{proxy}")

    def selector_group_for_host(
        self,
        host: str,
        proxies: dict[str, dict[str, Any]] | None = None,
    ) -> str | None:
        """根据当前生效规则找到指定域名实际使用的 Selector。"""
        host = host.strip().lower().rstrip(".")
        if not host:
            return None
        proxies = proxies if proxies is not None else self.proxies()
        rules = self.request("GET", "/rules").get("rules", [])
        for rule in sorted(
            (item for item in rules if isinstance(item, dict)),
            key=lambda item: int(item.get("index", 0)),
        ):
            rule_type = str(rule.get("type") or "").casefold()
            payload = str(rule.get("payload") or "").strip().lower().rstrip(".")
            matched = False
            if rule_type == "domain":
                matched = host == payload
            elif rule_type == "domainsuffix":
                matched = host == payload or host.endswith(f".{payload}")
            elif rule_type == "domainkeyword":
                matched = bool(payload) and payload in host
            elif rule_type in {"match", "final"}:
                matched = True
            if not matched:
                continue
            proxy = str(rule.get("proxy") or "")
            if proxies.get(proxy, {}).get("type") == "Selector":
                return proxy
            # DIRECT、REJECT 和下游非 Selector 组都不能作为切换目标。
            return None
        return None

    def web_probe(
        self,
        url: str,
        timeout_ms: int,
        allowed_hosts: list[str] | None = None,
        blocked_url_keywords: list[str] | None = None,
    ) -> dict[str, Any]:
        """通过 Mihomo 混合端口检查目标网页的公开跳转。"""
        global _SHARED_BROWSER_PROBE, _BROWSER_PROBE_UNAVAILABLE_AT
        configs = self.request("GET", "/configs")
        port = int(configs.get("mixed-port") or 0)
        if port <= 0:
            raise MihomoError("运行配置没有可用的 mixed-port，无法执行网页地区探测")
        proxy_url = f"http://127.0.0.1:{port}"
        target_host = (urllib.parse.urlparse(url).hostname or "").lower()
        if target_host == "aistudio.google.com" and self._aistudio_api_keys:
            api_result = self._aistudio_api_probe(proxy_url, "", timeout_ms)
            if api_result is not None:
                return {
                    "status": 200,
                    "final_url": url,
                    "elapsed_ms": 0,
                    **api_result,
                }
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/136 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
            },
        )
        # 带上自动发现的 Playwright 登录态。没有 Cookie 时，AI Studio 常会
        # 返回一个公开的 200 页面，无法反映真实浏览器最终跳转的地区限制页。
        if target_host.endswith(".google.com") or target_host == "google.com":
            storage_state = find_storage_state()
            if storage_state is not None:
                credentials = self._storage_cookie_header(
                    storage_state,
                    target_host,
                    require_sapisid=False,
                )
                if credentials is not None:
                    request.add_header("Cookie", credentials[0])
        started = time.monotonic()
        try:
            with opener.open(request, timeout=max(1.0, timeout_ms / 1000)) as response:
                status = response.status
                final_url = response.geturl()
                body = _read_response_body(response, 512 * 1024)
        except urllib.error.HTTPError as exc:
            status = exc.code
            final_url = exc.geturl()
            body = _read_response_body(exc, 512 * 1024)
        except Exception as exc:
            raise MihomoError(f"目标网页探测失败：{exc}") from exc
        elapsed_ms = round((time.monotonic() - started) * 1000)
        parsed = urllib.parse.urlparse(final_url)
        host = (parsed.hostname or "").lower()
        lower_final_url = final_url.lower()
        lower_body = body.lower()
        region_markers = (
            "user location is not supported",
            "available regions for google ai studio and gemini api",
        )
        if status < 200 or status >= 400:
            raise MihomoError(f"目标网页返回 HTTP {status}，最终地址：{final_url}")
        blocked_keywords = [item.strip().lower() for item in (blocked_url_keywords or ["available-regions"]) if item.strip()]
        if any(keyword in lower_final_url for keyword in blocked_keywords):
            raise MihomoError(f"出口地区不受支持，跳转到：{final_url}")
        if any(marker in lower_body for marker in region_markers):
            raise MihomoError("目标网页包含地区不受支持提示")
        allowed = {item.strip().lower() for item in (allowed_hosts or ["aistudio.google.com", "accounts.google.com"]) if item.strip()}
        if host not in allowed:
            raise MihomoError(f"目标网页跳转到非预期域名：{final_url}")
        result = {"status": status, "final_url": final_url, "elapsed_ms": elapsed_ms}
        if host == "aistudio.google.com" and parsed.path.lower() != "/docs/available-regions":
            api_result = self._aistudio_api_probe(proxy_url, body, timeout_ms)
            if api_result is not None:
                result.update(api_result)
            else:
                browser_probe = self._get_browser_probe()
                if not browser_probe:
                    return result
                try:
                    # 浏览器只是无法由 RPC 判定时的最后回退；全局串行化，避免多个
                    # MihomoClient 同时拉起完整浏览器导致内存峰值失控。
                    with _BROWSER_PROBE_CALL_LOCK:
                        browser_result = browser_probe.probe(url, proxy_url, timeout_ms)
                except BrowserProbeUnavailable:
                    with _BROWSER_PROBE_LOCK:
                        _SHARED_BROWSER_PROBE = False
                        _BROWSER_PROBE_UNAVAILABLE_AT = time.monotonic()
                    self._browser_probe = False
                except BrowserProbeError as exc:
                    raise MihomoError(str(exc)) from exc
                else:
                    result.update(browser_result)
        return result

    @staticmethod
    def _storage_cookie_header(
        storage_state: pathlib.Path,
        host: str,
        require_sapisid: bool = True,
    ) -> tuple[str, str] | None:
        try:
            data = json.loads(storage_state.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        now = time.time()
        cookies: dict[str, tuple[int, str]] = {}
        for item in data.get("cookies", []):
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or "").lstrip(".").lower()
            if not domain or (host != domain and not host.endswith(f".{domain}")):
                continue
            expires = item.get("expires")
            try:
                if expires and float(expires) < now:
                    continue
            except (TypeError, ValueError):
                continue
            name = str(item.get("name") or "")
            value = str(item.get("value") or "")
            if not name:
                continue
            specificity = len(domain)
            previous = cookies.get(name)
            if previous is None or specificity >= previous[0]:
                cookies[name] = (specificity, value)
        values = {name: value for name, (_, value) in cookies.items()}
        sapisid = values.get("SAPISID") or values.get("__Secure-3PAPISID") or values.get("APISID")
        cookie_header = "; ".join(f"{name}={value}" for name, value in values.items())
        if not cookie_header or (require_sapisid and not sapisid):
            return None
        return cookie_header, sapisid

    def _aistudio_api_probe(
        self,
        proxy_url: str,
        page_body: str,
        timeout_ms: int,
    ) -> dict[str, object] | None:
        """直接复核 AI Studio 登录后使用的 ListModels RPC，避免启动完整浏览器。"""
        storage_state = find_storage_state()
        if storage_state is None:
            return None
        credentials = self._storage_cookie_header(storage_state, "alkalimakersuite-pa.clients6.google.com")
        if credentials is None or not credentials[1]:
            return None
        cookie_header, sapisid = credentials
        api_keys = list(self._aistudio_api_keys)
        api_keys.extend(re.findall(r"AIza[0-9A-Za-z_-]{20,}", page_body))
        api_keys = list(dict.fromkeys(api_keys))
        if not api_keys:
            return None

        endpoint = (
            "https://alkalimakersuite-pa.clients6.google.com/"
            "$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/ListModels"
        )
        timestamp = str(int(time.time()))
        digest = hashlib.sha1(
            f"{timestamp} {sapisid} https://aistudio.google.com".encode("utf-8")
        ).hexdigest()
        authorization = (
            f"SAPISIDHASH {timestamp}_{digest} "
            f"SAPISID1PHASH {timestamp}_{digest} "
            f"SAPISID3PHASH {timestamp}_{digest}"
        )
        headers_base = {
            "Accept": "*/*",
            "Authorization": authorization,
            "Content-Type": "application/json+protobuf",
            "Cookie": cookie_header,
            "Origin": "https://aistudio.google.com",
            "Referer": "https://aistudio.google.com/",
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/153 Safari/537.36",
            "X-Goog-Authuser": "0",
            "X-User-Agent": "grpc-web-javascript/0.1",
        }
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
        last_error = ""
        for api_key in api_keys:
            headers = dict(headers_base)
            headers["X-Goog-Api-Key"] = api_key
            request = urllib.request.Request(endpoint, data=b"[]", method="POST", headers=headers)
            try:
                with opener.open(request, timeout=max(1.0, timeout_ms / 1000)) as response:
                    api_status = response.status
                    api_body = response.read(64 * 1024).decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                api_status = exc.code
                api_body = exc.read(64 * 1024).decode("utf-8", errors="replace")
            except Exception as exc:
                last_error = str(exc)
                continue

            lowered = api_body.lower()
            # “Permission denied” 也可能是账号或 API key 的全局问题，不能仅凭
            # 这一句把所有节点都判死；网页复核会继续确认真实地区跳转。
            region_error = any(marker in lowered for marker in ("available regions",)) or (
                any(marker in lowered for marker in ("region", "location", "account"))
                and any(marker in lowered for marker in ("not supported", "unsupported"))
            )
            if region_error:
                self._aistudio_api_keys = (api_key,)
                raise MihomoError(
                    f"AI Studio API 返回 HTTP {api_status}：{api_body[:240] or 'Region not supported.'}"
                )
            if 200 <= api_status < 300:
                self._aistudio_api_keys = (api_key,)
                return {"api_probe": "ok", "api_status": api_status}
            last_error = f"HTTP {api_status}: {api_body[:160]}"

        # API key 可能随前端版本变化；清除失效缓存后交给网页或浏览器回退确认。
        if self._aistudio_api_keys:
            self._aistudio_api_keys = ()
        if last_error:
            logging.debug("AI Studio API key 均未通过：%s", last_error)
        return None

    def _get_browser_probe(self) -> BrowserProbe | None:
        global _SHARED_BROWSER_PROBE, _BROWSER_PROBE_UNAVAILABLE_AT
        now = time.monotonic()
        if self._browser_probe is False and now - _BROWSER_PROBE_UNAVAILABLE_AT < _BROWSER_PROBE_RETRY_SECONDS:
            return None
        with _BROWSER_PROBE_LOCK:
            if (
                _SHARED_BROWSER_PROBE is False
                and now - _BROWSER_PROBE_UNAVAILABLE_AT >= _BROWSER_PROBE_RETRY_SECONDS
            ):
                _SHARED_BROWSER_PROBE = None
                self._browser_probe = None
            if self._browser_probe is None:
                if _SHARED_BROWSER_PROBE is None:
                    try:
                        _SHARED_BROWSER_PROBE = BrowserProbe(
                            timeout_seconds=max(8.0, self.timeout + 2.0)
                        )
                    except BrowserProbeUnavailable:
                        _SHARED_BROWSER_PROBE = False
                        _BROWSER_PROBE_UNAVAILABLE_AT = now
                self._browser_probe = _SHARED_BROWSER_PROBE
        return self._browser_probe if isinstance(self._browser_probe, BrowserProbe) else None

    def candidates(self, group: str) -> list[dict[str, str]]:
        return self.selector_group_candidates(group)[1]
