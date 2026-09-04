from __future__ import annotations

import json
import ctypes
import os
import pathlib
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import yaml


DEFAULT_WINDOWS_PIPE_PATH = r"\\.\pipe\verge-mihomo"
WINDOWS_PIPE_MAX_CONCURRENCY = 16
WINDOWS_PIPE_OPEN_RETRIES = 3
WINDOWS_PIPE_RETRY_DELAY_SECONDS = 0.1
_WINDOWS_PIPE_SEMAPHORE = threading.BoundedSemaphore(WINDOWS_PIPE_MAX_CONCURRENCY)


class MihomoError(RuntimeError):
    """Mihomo 控制接口调用失败。"""


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
    if not socket_path and os.name != "nt":
        for candidate in ("/tmp/verge/verge-mihomo.sock", "/tmp/clash-verge-rev/verge-mihomo.sock"):
            if pathlib.Path(candidate).exists():
                socket_path = candidate
                break
    if os.name == "nt" and not pipe_path:
        pipe_path = DEFAULT_WINDOWS_PIPE_PATH
    if url and "://" not in url:
        url = f"http://{url}"
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

    @staticmethod
    def _windows_pipe_error(error_code: int, path: str) -> OSError:
        message = ctypes.FormatError(error_code).strip() or "Windows 命名管道操作失败"
        return OSError(error_code, message, path)

    @classmethod
    def _open_windows_pipe(cls, path: str, timeout: float):
        """使用 Win32 API 打开命名管道，避免 CRT open 的并发兼容性问题。"""
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
                if last_error not in retryable_errors:
                    break
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
                time.sleep(min(WINDOWS_PIPE_RETRY_DELAY_SECONDS * (attempt + 1), max(0.0, deadline - time.monotonic())))

        raise cls._windows_pipe_error(last_error, path)

    def _windows_pipe_request(self, raw_request: bytes) -> tuple[int, bytes]:
        """限制本进程的管道并发，并在管道暂时繁忙时重试打开。"""
        if not self.settings.pipe_path:
            raise MihomoError("未配置 Clash Verge Rev 命名管道")
        acquired = _WINDOWS_PIPE_SEMAPHORE.acquire(timeout=max(0.1, self.timeout))
        if not acquired:
            raise MihomoError("等待 Clash Verge Rev 命名管道并发槽位超时")
        try:
            try:
                with self._open_windows_pipe(self.settings.pipe_path, self.timeout) as pipe:
                    pipe.write(raw_request)
                    pipe.flush()
                    return _decode_http(pipe.read())
            except OSError as exc:
                raise MihomoError(f"无法连接 Clash Verge Rev 命名管道：{exc}") from exc
        finally:
            _WINDOWS_PIPE_SEMAPHORE.release()

    def _http_request(self, method: str, path: str, payload: bytes | None) -> tuple[int, bytes]:
        url = self.settings.url.rstrip("/") + path
        headers = {"Accept": "application/json"}
        if self.settings.secret:
            headers["Authorization"] = f"Bearer {self.settings.secret}"
        request = urllib.request.Request(url, data=payload, method=method, headers=headers)
        if payload is not None:
            request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.status, response.read()

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
                raise MihomoError(f"无法连接 Mihomo Unix Socket：{exc}") from exc
        raise MihomoError("未发现可用的 Mihomo 控制器")

    def request(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
        errors: list[str] = []
        status: int | None = None
        body = b""

        # Windows 优先使用 Clash Verge Rev 的本地管道，避免访问通常未开启的 TCP 控制器。
        if os.name == "nt" and self.settings.pipe_path:
            try:
                status, body = self._raw_request(method, path, payload)
            except MihomoError as exc:
                errors.append(f"命名管道：{exc}")

        if status is None and self.settings.url:
            try:
                status, body = self._http_request(method, path, payload)
            except Exception as exc:
                errors.append(f"外部控制器：{exc}")

        if status is None and os.name != "nt" and self.settings.socket_path:
            try:
                status, body = self._raw_request(method, path, payload)
            except MihomoError as exc:
                errors.append(str(exc))

        if status is None:
            if errors:
                raise MihomoError("；".join(errors))
            raise MihomoError("未发现可用的 Mihomo 控制器")
        if status < 200 or status >= 300:
            message = body.decode("utf-8", errors="replace")
            raise MihomoError(f"控制器返回 HTTP {status}：{message}")
        if not body.strip():
            return None
        return json.loads(body)

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

    def selector_group_candidates(self, group: str) -> tuple[str, list[dict[str, str]]]:
        """读取策略组当前节点及其可选的真实节点。"""
        proxies = self.proxies()
        group_data = proxies.get(group, {})
        if group_data.get("type") != "Selector":
            raise MihomoError(f"找不到 Selector 策略组：{group}")

        candidates = []
        for name in group_data.get("all", []):
            item = proxies.get(name, {})
            if item.get("type") in {"Selector", "URLTest", "Fallback", "LoadBalance", "Direct", "Reject"}:
                continue
            candidates.append({"name": name, "provider": str(item.get("provider-name") or "本地配置")})
        return str(group_data.get("now") or ""), candidates

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

    def web_probe(
        self,
        url: str,
        timeout_ms: int,
        allowed_hosts: list[str] | None = None,
        blocked_url_keywords: list[str] | None = None,
    ) -> dict[str, Any]:
        """通过 Mihomo 混合端口检查目标网页的公开跳转。"""
        configs = self.request("GET", "/configs")
        port = int(configs.get("mixed-port") or 0)
        if port <= 0:
            raise MihomoError("运行配置没有可用的 mixed-port，无法执行网页地区探测")
        proxy_url = f"http://127.0.0.1:{port}"
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
        started = time.monotonic()
        try:
            with opener.open(request, timeout=max(1.0, timeout_ms / 1000)) as response:
                status = response.status
                final_url = response.geturl()
                body = response.read(512 * 1024).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            status = exc.code
            final_url = exc.geturl()
            body = exc.read(512 * 1024).decode("utf-8", errors="replace")
        except Exception as exc:
            raise MihomoError(f"目标网页探测失败：{exc}") from exc
        elapsed_ms = round((time.monotonic() - started) * 1000)
        parsed = urllib.parse.urlparse(final_url)
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()
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
        return {"status": status, "final_url": final_url, "elapsed_ms": elapsed_ms}

    def candidates(self, group: str) -> list[dict[str, str]]:
        return self.selector_group_candidates(group)[1]
