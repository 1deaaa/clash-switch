from __future__ import annotations

import os
import pathlib
import shutil
import signal
import subprocess
import sys


SYSTEMD_USER_UNIT_NAME = "clash-verge-node-guard.service"


class ServiceManagerError(RuntimeError):
    """后台服务管理失败。"""


def _quote_systemd(value: str) -> str:
    """按 systemd 单元语法引用路径。"""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class MonitorServiceManager:
    """统一管理监控进程及 Linux 用户级 systemd 单元。"""

    def __init__(
        self,
        root: pathlib.Path,
        interpreter: str | None = None,
        unit_path: pathlib.Path | None = None,
    ) -> None:
        self.root = root
        self.interpreter = interpreter or sys.executable
        self.script_path = root / "monitor_service.py"
        self.pid_path = root / "monitor.pid"
        config_home = pathlib.Path(os.environ.get("XDG_CONFIG_HOME", pathlib.Path.home() / ".config"))
        self.unit_path = unit_path or config_home / "systemd/user" / SYSTEMD_USER_UNIT_NAME
        self._systemd_available: bool | None = None

    def _run_systemctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["systemctl", "--user", *args],
            text=True,
            capture_output=True,
            check=False,
        )

    def _can_use_systemd(self) -> bool:
        if self._systemd_available is None:
            if os.name == "nt" or shutil.which("systemctl") is None:
                self._systemd_available = False
            else:
                self._systemd_available = self._run_systemctl("show-environment").returncode == 0
        return self._systemd_available

    def _unit_contents(self) -> str:
        return "\n".join(
            (
                "[Unit]",
                "Description=Clash Verge 节点守护",
                "After=graphical-session.target",
                "",
                "[Service]",
                "Type=simple",
                f"WorkingDirectory={self.root}",
                f"ExecStart={_quote_systemd(self.interpreter)} {_quote_systemd(str(self.script_path))}",
                "Restart=on-failure",
                "RestartSec=5",
                "",
                "[Install]",
                "WantedBy=default.target",
                "",
            )
        )

    @staticmethod
    def _check_result(result: subprocess.CompletedProcess[str], action: str) -> None:
        if result.returncode == 0:
            return
        detail = (result.stderr or result.stdout).strip()
        suffix = f"：{detail}" if detail else ""
        raise ServiceManagerError(f"{action}失败{suffix}")

    def _pid_is_running(self) -> bool:
        if not self.pid_path.exists():
            return False
        try:
            pid = int(self.pid_path.read_text(encoding="ascii"))
            os.kill(pid, 0)
            return True
        except (ValueError, OSError):
            self.pid_path.unlink(missing_ok=True)
            return False

    def _stop_pid_process(self) -> None:
        if not self._pid_is_running():
            return
        pid = int(self.pid_path.read_text(encoding="ascii"))
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        else:
            os.kill(pid, signal.SIGTERM)
        self.pid_path.unlink(missing_ok=True)

    def is_systemd_managed(self) -> bool:
        return self._can_use_systemd() and self.unit_path.exists()

    def is_running(self) -> bool:
        if self.is_systemd_managed():
            result = self._run_systemctl("is-active", "--quiet", SYSTEMD_USER_UNIT_NAME)
            if result.returncode == 0:
                return True
        return self._pid_is_running()

    def install_and_enable(self) -> None:
        self.unit_path.parent.mkdir(parents=True, exist_ok=True)
        unit_contents = self._unit_contents()
        if not self.unit_path.exists() or self.unit_path.read_text(encoding="utf-8") != unit_contents:
            self.unit_path.write_text(unit_contents, encoding="utf-8")
        self._check_result(self._run_systemctl("daemon-reload"), "重新加载用户服务配置")
        self._check_result(
            self._run_systemctl("enable", "--now", SYSTEMD_USER_UNIT_NAME),
            "启用并启动用户服务",
        )

    def start(self) -> subprocess.Popen[str] | None:
        if self._can_use_systemd():
            self.install_and_enable()
            return None
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return subprocess.Popen(
            [self.interpreter, str(self.script_path)],
            cwd=self.root,
            creationflags=flags,
        )

    def stop(self) -> None:
        if self.is_systemd_managed():
            self._check_result(self._run_systemctl("stop", SYSTEMD_USER_UNIT_NAME), "停止用户服务")
        self._stop_pid_process()
