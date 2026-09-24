from __future__ import annotations

import json
import atexit
import os
import pathlib
import select
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass


class BrowserProbeUnavailable(RuntimeError):
    """本机没有可用于网页复核的浏览器或登录配置。"""


class BrowserProbeError(RuntimeError):
    """浏览器已经启动，但目标页面没有通过复核。"""


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    executable: pathlib.Path
    user_data_dir: pathlib.Path
    profile_dir: pathlib.Path


@dataclass(frozen=True, slots=True)
class BrowserRunnerProfile:
    node: pathlib.Path
    playwright_root: pathlib.Path
    executable: pathlib.Path
    storage_state: pathlib.Path
    engine: str


def _unique_paths(paths: list[pathlib.Path]) -> list[pathlib.Path]:
    result: list[pathlib.Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _browser_profiles() -> list[BrowserProfile]:
    home = pathlib.Path.home()
    if os.name == "nt":
        local_app_data = pathlib.Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        roots = [
            (local_app_data / "Microsoft/Edge/User Data", ["msedge.exe", "msedge"]),
            (local_app_data / "Google/Chrome/User Data", ["chrome.exe", "chrome"]),
        ]
    elif sys_platform() == "darwin":
        roots = [
            (home / "Library/Application Support/Microsoft Edge", ["Microsoft Edge"]),
            (home / "Library/Application Support/Google/Chrome", ["Google Chrome"]),
        ]
    else:
        roots = [
            (home / ".config/microsoft-edge", ["microsoft-edge", "msedge"]),
            (home / ".config/google-chrome", ["google-chrome", "chrome"]),
            (home / ".config/chromium", ["chromium", "chromium-browser"]),
        ]

    explicit = os.environ.get("CLASH_SWITCH_BROWSER", "").strip()
    executables: list[pathlib.Path] = []
    if explicit:
        executables.append(pathlib.Path(explicit).expanduser())
    for _, names in roots:
        for name in names:
            resolved = shutil.which(name)
            if resolved:
                executables.append(pathlib.Path(resolved))
    executables.extend(
        path
        for path in (
            pathlib.Path("/opt/microsoft/msedge/msedge"),
            pathlib.Path("/usr/bin/microsoft-edge"),
            pathlib.Path("/usr/bin/google-chrome"),
            pathlib.Path("/usr/bin/chromium"),
        )
        if path.exists()
    )
    executables = _unique_paths(executables)

    profiles: list[BrowserProfile] = []
    for root, _ in roots:
        if not (root / "Local State").exists():
            continue
        profile_dir = _default_profile_dir(root)
        if profile_dir is None:
            continue
        executable = next((item for item in executables if item.exists()), None)
        if executable is None:
            continue
        profiles.append(BrowserProfile(executable, root, profile_dir))
    return profiles


def _auth_state_candidates() -> list[pathlib.Path]:
    explicit = os.environ.get("CLASH_SWITCH_STORAGE_STATE", "").strip()
    if explicit:
        path = pathlib.Path(explicit).expanduser()
        return [path] if path.is_file() else []

    home = pathlib.Path.home()
    roots = [
        home / "web/AIStudioToAPI/configs/auth",
        home / "Desktop/web/AIStudioToAPI/configs/auth",
        home / "Desktop/AIStudioToAPI/configs/auth",
    ]
    candidates = []
    for root in roots:
        if root.is_dir():
            candidates.extend(root.glob("auth-*.json"))
    return sorted(
        (path for path in candidates if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )


def find_storage_state() -> pathlib.Path | None:
    """返回最新的 Playwright 登录态文件，供轻量 API 复核使用。"""
    return next(iter(_auth_state_candidates()), None)


def _playwright_roots(storage_state: pathlib.Path) -> list[pathlib.Path]:
    explicit = os.environ.get("CLASH_SWITCH_PLAYWRIGHT_ROOT", "").strip()
    roots = [pathlib.Path(explicit).expanduser()] if explicit else []
    project_root = storage_state.parent.parent.parent
    roots.extend(
        [
            project_root / "node_modules/playwright",
            project_root / "node_modules/playwright-core",
        ]
    )
    return [path for path in _unique_paths(roots) if path.exists()]


def _browser_runner_profile() -> BrowserRunnerProfile | None:
    explicit_node = os.environ.get("CLASH_SWITCH_NODE", "").strip()
    resolved_node = explicit_node or shutil.which("node")
    if not resolved_node:
        return None
    node = pathlib.Path(resolved_node).expanduser()
    if not node.is_file():
        return None

    executable_override = os.environ.get("CLASH_SWITCH_BROWSER", "").strip()
    executables = [pathlib.Path(executable_override).expanduser()] if executable_override else []
    executables.extend(
        pathlib.Path(path)
        for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser")
        if (path := shutil.which(name))
    )
    camoufox_override = os.environ.get("CLASH_SWITCH_CAMOUFOX_EXECUTABLE", "").strip()
    if camoufox_override:
        executables.insert(0, pathlib.Path(camoufox_override).expanduser())
    executables.extend(
        path
        for path in (
            pathlib.Path("/home/aidea/web/AIStudioToAPI/camoufox-linux/camoufox"),
            pathlib.Path("/home/aidea/Desktop/web/AIStudioToAPI/camoufox-linux/camoufox"),
        )
        if path.exists()
    )
    executables = [path for path in _unique_paths(executables) if path.exists()]
    if not executables:
        return None

    for storage_state in _auth_state_candidates():
        for playwright_root in _playwright_roots(storage_state):
            executable = executables[0]
            engine = "firefox" if "camoufox" in executable.name.lower() else "chromium"
            return BrowserRunnerProfile(node, playwright_root, executable, storage_state, engine)
    return None


def sys_platform() -> str:
    """延迟读取平台名称，避免引入会改变启动路径的第三方依赖。"""
    import sys

    return sys.platform


def _default_profile_dir(user_data_dir: pathlib.Path) -> pathlib.Path | None:
    default = user_data_dir / "Default"
    if default.is_dir():
        return default
    local_state = user_data_dir / "Local State"
    try:
        data = json.loads(local_state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    profile_info = data.get("profile", {}).get("info_cache", {})
    for name in profile_info:
        candidate = user_data_dir / str(name)
        if candidate.is_dir():
            return candidate
    return None


class BrowserProbe:
    """使用本机浏览器登录态执行一次真实的 AI Studio 页面复核。"""

    def __init__(self, timeout_seconds: float = 12.0, virtual_time_budget_ms: int = 5000):
        profiles = _browser_profiles()
        self.runner_profile = _browser_runner_profile()
        if not profiles and self.runner_profile is None:
            raise BrowserProbeUnavailable("没有发现可用的 Chromium 浏览器登录配置")
        self.source = profiles[0] if profiles else None
        self.timeout_seconds = max(6.0, timeout_seconds)
        self.virtual_time_budget_ms = max(1000, virtual_time_budget_ms)
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._probe_dir: pathlib.Path | None = None
        self._lock = threading.Lock()
        self._runner: subprocess.Popen[str] | None = None
        atexit.register(self.close)

    def _ensure_probe_profile(self) -> pathlib.Path:
        if self.source is None:
            raise BrowserProbeUnavailable("没有发现 Chromium 浏览器登录配置")
        if self._probe_dir is not None:
            return self._probe_dir
        self._temporary = tempfile.TemporaryDirectory(prefix="clash-switch-browser-")
        probe_dir = pathlib.Path(self._temporary.name)
        probe_profile = probe_dir / self.source.profile_dir.name
        probe_profile.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.source.user_data_dir / "Local State", probe_dir / "Local State")
        for name in ("Preferences", "Network Persistent State", "Trust Tokens", "Web Data"):
            source = self.source.profile_dir / name
            if source.exists():
                shutil.copy2(source, probe_profile / name)
        local_storage = self.source.profile_dir / "Local Storage"
        if local_storage.is_dir():
            shutil.copytree(
                local_storage,
                probe_profile / "Local Storage",
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("LOCK"),
            )
        self._copy_cookies(probe_profile)
        self._probe_dir = probe_dir
        return probe_dir

    def _copy_cookies(self, probe_profile: pathlib.Path) -> None:
        for suffix in ("", "-wal", "-shm"):
            source = self.source.profile_dir / f"Cookies{suffix}"
            target = probe_profile / f"Cookies{suffix}"
            try:
                if source.exists():
                    shutil.copy2(source, target)
                elif target.exists():
                    target.unlink()
            except OSError:
                # 浏览器正在写 Cookie 时，继续使用临时配置中上一次的有效副本。
                continue

    @staticmethod
    def _target_url(url: str) -> str:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.urlencode({"clash_switch_probe": str(time.time_ns())})
        return urllib.parse.urlunsplit(
            (parsed.scheme or "https", parsed.netloc or "aistudio.google.com", "/prompts/new_chat", query, "")
        )

    @staticmethod
    def _is_region_blocked(dom: str) -> bool:
        lowered = dom.lower()
        markers = (
            "/docs/available-regions",
            "available-regions",
            "available regions for google ai studio and gemini api",
            "region not supported",
            "account not supported",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _is_authenticated_page(dom: str) -> bool:
        lowered = dom.lower()
        return "<app-root" in lowered and "google ai studio" in lowered

    def _start_runner(self) -> subprocess.Popen[str]:
        latest_profile = _browser_runner_profile()
        if latest_profile is not None and latest_profile != self.runner_profile:
            self._stop_runner()
            self.runner_profile = latest_profile
        if self.runner_profile is None:
            raise BrowserProbeUnavailable("没有找到可复用的 Playwright 登录配置")
        if self._runner is not None and self._runner.poll() is None:
            return self._runner
        command = [
            str(self.runner_profile.node),
            str(pathlib.Path(__file__).with_name("browser_probe_runner.js")),
            "--playwright-root",
            str(self.runner_profile.playwright_root),
            "--executable",
            str(self.runner_profile.executable),
            "--storage-state",
            str(self.runner_profile.storage_state),
            "--engine",
            self.runner_profile.engine,
        ]
        try:
            self._runner = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            raise BrowserProbeUnavailable(f"无法启动 Playwright 浏览器复核：{exc}") from exc
        return self._runner

    def _stop_runner(self) -> None:
        """停止失步或超时的持久浏览器进程。"""
        runner = self._runner
        self._runner = None
        if runner is None or runner.poll() is not None:
            return
        try:
            os.killpg(runner.pid, signal.SIGTERM)
        except OSError:
            try:
                runner.terminate()
            except OSError:
                pass
        try:
            runner.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(runner.pid, signal.SIGKILL)
            except OSError:
                runner.kill()
            runner.wait()

    def _runner_probe(self, url: str, proxy_url: str, timeout_ms: int) -> dict[str, object]:
        runner = self._start_runner()
        if runner.stdin is None or runner.stdout is None:
            raise BrowserProbeUnavailable("浏览器复核进程管道不可用")
        request = json.dumps(
            {"url": url, "proxy": proxy_url, "timeoutMs": timeout_ms},
            ensure_ascii=False,
        )
        try:
            runner.stdin.write(request + "\n")
            runner.stdin.flush()
            ready, _, _ = select.select([runner.stdout], [], [], max(self.timeout_seconds + 4.0, timeout_ms / 1000 + 6.0))
            if not ready:
                raise BrowserProbeError("AI Studio 浏览器页面复核超时")
            line = runner.stdout.readline()
            if not line:
                raise BrowserProbeUnavailable("浏览器复核进程已退出")
            result = json.loads(line)
        except BrowserProbeError:
            self._stop_runner()
            raise
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._stop_runner()
            raise BrowserProbeUnavailable(f"读取浏览器复核结果失败：{exc}") from exc
        if result.get("unavailable"):
            self._stop_runner()
            raise BrowserProbeUnavailable(str(result.get("detail") or "浏览器复核不可用"))
        if result.get("blocked"):
            raise BrowserProbeError(
                f"浏览器页面被 AI Studio 拒绝（{result.get('detail') or result.get('url') or '地区不支持'}）"
            )
        if not result.get("ok"):
            raise BrowserProbeError(str(result.get("detail") or "AI Studio 浏览器页面复核失败"))
        return {
            "status": 200,
            "final_url": str(result.get("url") or self._target_url(url)),
            "elapsed_ms": int(result.get("elapsedMs") or 0),
            "browser_probe": "ok",
        }

    def probe(self, url: str, proxy_url: str, timeout_ms: int) -> dict[str, object]:
        with self._lock:
            if self.runner_profile is not None:
                return self._runner_probe(url, proxy_url, timeout_ms)
            probe_dir = self._ensure_probe_profile()
            self._copy_cookies(probe_dir / self.source.profile_dir.name)
            target_url = self._target_url(url)
            command = [
                str(self.source.executable),
                "--headless=new",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-sync",
                "--no-sandbox",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-quic",
                f"--virtual-time-budget={self.virtual_time_budget_ms}",
                f"--user-data-dir={probe_dir}",
                f"--profile-directory={self.source.profile_dir.name}",
                f"--proxy-server={proxy_url}",
                "--dump-dom",
                target_url,
            ]
            started = time.monotonic()
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            try:
                dom, _ = process.communicate(timeout=max(self.timeout_seconds, timeout_ms / 1000 + 2.0))
            except subprocess.TimeoutExpired as exc:
                output = exc.stdout or ""
                if isinstance(output, bytes):
                    output = output.decode("utf-8", errors="replace")
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    process.kill()
                process.communicate()
                if self._is_region_blocked(output):
                    raise BrowserProbeError("浏览器页面跳转到 AI Studio 地区不支持页") from exc
                raise BrowserProbeError("AI Studio 浏览器页面复核超时") from exc
            except OSError as exc:
                process.kill()
                process.communicate()
                raise BrowserProbeUnavailable(f"无法启动网页复核浏览器：{exc}") from exc

            if self._is_region_blocked(dom):
                raise BrowserProbeError("浏览器页面跳转到 AI Studio 地区不支持页")
            if not self._is_authenticated_page(dom):
                detail = "浏览器没有加载 AI Studio 页面"
                if process.returncode:
                    detail += f"（退出码 {process.returncode}）"
                raise BrowserProbeError(detail)
            return {
                "status": 200,
                "final_url": target_url,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "browser_probe": "ok",
            }

    def close(self) -> None:
        self._stop_runner()
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
        self._probe_dir = None


__all__ = [
    "BrowserProbe",
    "BrowserProbeError",
    "BrowserProbeUnavailable",
    "find_storage_state",
]
