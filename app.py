from __future__ import annotations

import json
import copy
import os
import pathlib
import signal
import subprocess
import sys
import threading
import tkinter as tk
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from tkinter import messagebox

import customtkinter as ctk

from mihomo_client import ControllerSettings, MihomoClient
from monitor_service import (
    CONFIG_PATH,
    LOG_PATH,
    PID_PATH,
    MonitorConfig,
    probe_node_delays,
    probe_selected_web,
)


ROOT = pathlib.Path(__file__).resolve().parent


def normalize_allowed_hosts(test_urls: list[str], raw_hosts: str) -> list[str]:
    """规范允许域名，并自动加入测试网址自身的主机名。"""
    hosts = [item.strip().lower() for item in raw_hosts.split(",") if item.strip()]
    for test_url in test_urls:
        test_host = urllib.parse.urlparse(test_url).hostname
        if test_host:
            hosts.append(test_host.lower())
    if any("://" in host or "/" in host for host in hosts):
        raise ValueError("允许最终域名只能填写主机名，不能包含协议或路径")
    return list(dict.fromkeys(hosts))


def test_group_candidates(client, config: MonitorConfig, candidates, on_result=None):
    """测试策略组全部候选，并在结束后恢复测试前的节点。"""
    group = client.selector_groups().get(config.group)
    if not group:
        raise ValueError(f"找不到策略组：{config.group}")
    original = str(group.get("now") or "")
    results = {}
    try:
        names = [item["name"] if isinstance(item, dict) else str(item) for item in candidates]

        def delay_probe(name):
            try:
                delay, delays = probe_node_delays(client, config, name)
                return name, {"status": "delay_ok", "delay": delay, "delays": delays}
            except Exception as exc:
                return name, {"status": "failed", "error": str(exc)}

        delay_results = {}
        with ThreadPoolExecutor(max_workers=max(1, min(16, len(names)))) as pool:
            futures = {pool.submit(delay_probe, name): name for name in names}
            for future in as_completed(futures):
                name, result = future.result()
                delay_results[name] = result
                if result["status"] == "failed":
                    result["final"] = True
                    if on_result:
                        on_result(name, result)
                else:
                    preview = dict(result)
                    preview["status"] = "ok"
                    preview["final"] = not config.advanced_web_probe
                    if on_result:
                        on_result(name, preview)

        for name in names:
            result = delay_results[name]
            passed_delay = result["status"] == "delay_ok"
            if passed_delay:
                try:
                    if config.advanced_web_probe:
                        client.select(config.group, name)
                        result["pages"] = probe_selected_web(client, config)
                    result["status"] = "ok"
                except Exception as exc:
                    result = {"status": "failed", "error": str(exc)}
            result["final"] = True
            results[name] = result
            if on_result and config.advanced_web_probe and passed_delay:
                on_result(name, result)
    finally:
        if config.advanced_web_probe and original:
            client.select(config.group, original)
    return results


class ConfigApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Clash Verge 节点守护")
        self.geometry("920x820")
        self.minsize(780, 720)
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")
        self.config_data = MonitorConfig.load()
        self.candidate_vars: dict[str, tk.BooleanVar] = {}
        self.node_status_labels = {}
        self.node_test_results = {}
        self.selected_candidates = set(self.config_data.candidates)
        self.provider_filter = "全部订阅"
        self.process: subprocess.Popen[str] | None = None
        self._build()
        self.after(200, self.refresh_groups)
        self.after(1000, self.refresh_status)

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        title = ctk.CTkLabel(self, text="Clash Verge 节点守护", font=ctk.CTkFont(size=24, weight="bold"))
        title.grid(row=0, column=0, padx=24, pady=(20, 12), sticky="w")

        settings = ctk.CTkFrame(self, corner_radius=6)
        settings.grid(row=1, column=0, padx=24, pady=0, sticky="ew")
        settings.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(settings, text="测试网址").grid(row=0, column=0, padx=14, pady=10, sticky="w")
        self.url_entry = ctk.CTkEntry(settings)
        self.url_entry.insert(0, ", ".join(self.config_data.test_urls))
        self.url_entry.grid(row=0, column=1, columnspan=3, padx=(0, 14), pady=10, sticky="ew")
        ctk.CTkLabel(settings, text="间隔（秒）").grid(row=1, column=0, padx=14, pady=10, sticky="w")
        self.interval_entry = ctk.CTkEntry(settings, width=100)
        self.interval_entry.insert(0, str(self.config_data.interval_seconds))
        self.interval_entry.grid(row=1, column=1, padx=(0, 14), pady=10, sticky="w")
        ctk.CTkLabel(settings, text="失败阈值").grid(row=1, column=2, padx=8, pady=10)
        self.threshold_entry = ctk.CTkEntry(settings, width=80)
        self.threshold_entry.insert(0, str(self.config_data.failure_threshold))
        self.threshold_entry.grid(row=1, column=3, padx=(0, 14), pady=10)

        self.advanced_probe_var = tk.BooleanVar(value=self.config_data.advanced_web_probe)
        self.advanced_probe_check = ctk.CTkCheckBox(
            settings,
            text="启用高级网页重定向判定",
            variable=self.advanced_probe_var,
            command=self._toggle_redirect_controls,
        )
        self.advanced_probe_check.grid(row=2, column=0, columnspan=2, padx=14, pady=(6, 8), sticky="w")
        ctk.CTkLabel(settings, text="允许最终域名").grid(row=3, column=0, padx=14, pady=8, sticky="w")
        self.allowed_hosts_entry = ctk.CTkEntry(settings, placeholder_text="多个域名用英文逗号分隔")
        self.allowed_hosts_entry.insert(0, ", ".join(self.config_data.allowed_redirect_hosts))
        self.allowed_hosts_entry.grid(row=3, column=1, columnspan=3, padx=(0, 14), pady=8, sticky="ew")
        ctk.CTkLabel(settings, text="拦截 URL 关键词").grid(row=4, column=0, padx=14, pady=(8, 12), sticky="w")
        self.blocked_keywords_entry = ctk.CTkEntry(settings, placeholder_text="例如 available-regions")
        self.blocked_keywords_entry.insert(0, ", ".join(self.config_data.blocked_url_keywords))
        self.blocked_keywords_entry.grid(row=4, column=1, columnspan=3, padx=(0, 14), pady=(8, 12), sticky="ew")
        self._toggle_redirect_controls()

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=2, column=0, padx=24, pady=14, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(2, weight=1)
        controls = ctk.CTkFrame(content, fg_color="transparent")
        controls.grid(row=0, column=0, sticky="ew")
        controls.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(controls, text="待切换策略组").grid(row=0, column=0, padx=(0, 8))
        self.group_menu = ctk.CTkOptionMenu(controls, values=["正在读取…"], command=lambda _: self.refresh_candidates())
        self.group_menu.grid(row=0, column=1, sticky="ew")
        self.refresh_button = ctk.CTkButton(controls, text="刷新", width=80, command=self.refresh_groups)
        self.refresh_button.grid(row=0, column=2, padx=(8, 0))
        ctk.CTkLabel(controls, text="订阅来源").grid(row=1, column=0, padx=(0, 8), pady=(10, 0))
        self.provider_menu = ctk.CTkOptionMenu(controls, values=["全部订阅"], command=self._filter_provider)
        self.provider_menu.grid(row=1, column=1, sticky="ew", pady=(10, 0))
        self.select_all_button = ctk.CTkButton(controls, text="全选/反选", width=80, command=self.toggle_all)
        self.select_all_button.grid(row=1, column=2, padx=(8, 0), pady=(10, 0))

        ctk.CTkLabel(content, text="候选节点（仅从当前运行配置抽取）").grid(row=1, column=0, pady=(12, 6), sticky="w")
        self.nodes_frame = ctk.CTkScrollableFrame(content, corner_radius=6)
        self.nodes_frame.grid(row=2, column=0, sticky="nsew")
        self.nodes_frame.grid_columnconfigure(0, weight=1)

        footer = ctk.CTkFrame(self, corner_radius=6)
        footer.grid(row=3, column=0, padx=24, pady=(0, 20), sticky="ew")
        footer.grid_columnconfigure(3, weight=1)
        self.save_button = ctk.CTkButton(footer, text="保存配置", command=self.save_config)
        self.save_button.grid(row=0, column=0, padx=10, pady=10)
        self.test_button = ctk.CTkButton(footer, text="立即测试", command=self.test_once)
        self.test_button.grid(row=0, column=1, padx=0, pady=10)
        self.service_button = ctk.CTkButton(footer, text="启动服务", command=self.toggle_service)
        self.service_button.grid(row=0, column=2, padx=10, pady=10)
        self.status_label = ctk.CTkLabel(footer, text="服务未运行")
        self.status_label.grid(row=0, column=3, padx=10, pady=10, sticky="e")
        self.log_box = ctk.CTkTextbox(footer, height=100)
        self.log_box.grid(row=1, column=0, columnspan=4, padx=10, pady=(0, 10), sticky="ew")

    def _client(self) -> MihomoClient:
        return MihomoClient(ControllerSettings(url=self.config_data.controller_url, socket_path=self.config_data.controller_socket))

    def _toggle_redirect_controls(self) -> None:
        state = "normal" if self.advanced_probe_var.get() else "disabled"
        for widget in (
            self.allowed_hosts_entry,
            self.blocked_keywords_entry,
        ):
            widget.configure(state=state)

    def _background(self, func, success=None) -> None:
        def worker() -> None:
            try:
                result = func()
                if success:
                    self.after(0, lambda: success(result))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("操作失败", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def refresh_groups(self) -> None:
        self.refresh_button.configure(state="disabled")
        def done(groups: dict) -> None:
            names = list(groups) or ["没有 Selector 策略组"]
            self.group_menu.configure(values=names)
            selected = self.config_data.group if self.config_data.group in groups else names[0]
            self.group_menu.set(selected)
            self.refresh_button.configure(state="normal")
            self.refresh_candidates()
        self._background(self._client().selector_groups, done)

    def refresh_candidates(self) -> None:
        group = self.group_menu.get()
        self.node_test_results = {}
        def done(items: list[dict[str, str]]) -> None:
            self._all_candidates = items
            providers = ["全部订阅"] + sorted({item["provider"] for item in items})
            self.provider_menu.configure(values=providers)
            if self.provider_filter not in providers:
                self.provider_filter = "全部订阅"
                self.provider_menu.set(self.provider_filter)
            self._render_candidates()
        self._background(lambda: self._client().candidates(group), done)

    def _filter_provider(self, value: str) -> None:
        self._capture_visible_selection()
        self.provider_filter = value
        self._render_candidates()

    def _capture_visible_selection(self) -> None:
        for name, var in self.candidate_vars.items():
            if var.get():
                self.selected_candidates.add(name)
            else:
                self.selected_candidates.discard(name)

    def _render_candidates(self) -> None:
        for child in self.nodes_frame.winfo_children():
            child.destroy()
        self.candidate_vars = {}
        shown = [item for item in getattr(self, "_all_candidates", []) if self.provider_filter == "全部订阅" or item["provider"] == self.provider_filter]
        for row, item in enumerate(shown):
            var = tk.BooleanVar(value=item["name"] in self.selected_candidates)
            self.candidate_vars[item["name"]] = var
            box = ctk.CTkCheckBox(self.nodes_frame, text=item["name"], variable=var)
            box.grid(row=row, column=0, padx=10, pady=5, sticky="w")
            result = self.node_test_results.get(item["name"])
            status_text = ""
            status_color = "gray60"
            if result and result["status"] == "testing":
                status_text = "测试中"
            elif result and result["status"] == "ok":
                status_text = f"{result['delay']} ms"
                status_color = "#16a34a"
            elif result:
                status_text = "失败"
                status_color = "#dc2626"
            status = ctk.CTkLabel(self.nodes_frame, text=status_text, text_color=status_color, width=72, anchor="e")
            status.grid(row=row, column=1, padx=(6, 2), pady=5, sticky="e")
            self.node_status_labels[item["name"]] = status
            ctk.CTkLabel(self.nodes_frame, text=item["provider"], text_color="gray60").grid(row=row, column=2, padx=(6, 10), pady=5, sticky="e")

    def toggle_all(self) -> None:
        target = not all(var.get() for var in self.candidate_vars.values())
        for var in self.candidate_vars.values():
            var.set(target)
        self._capture_visible_selection()

    def _collect_config(self, require_candidates: bool = True) -> MonitorConfig:
        try:
            interval = int(self.interval_entry.get())
            threshold = int(self.threshold_entry.get())
            if interval < 5 or threshold < 1:
                raise ValueError
        except ValueError:
            raise ValueError("间隔至少为 5 秒，失败阈值至少为 1")
        test_urls = [item.strip() for item in self.url_entry.get().split(",") if item.strip()]
        if not test_urls or any(not item.startswith(("http://", "https://")) for item in test_urls):
            raise ValueError("测试网址必须以 http:// 或 https:// 开头，多个网址用英文逗号分隔")
        self._capture_visible_selection()
        selected = [item["name"] for item in getattr(self, "_all_candidates", []) if item["name"] in self.selected_candidates]
        if require_candidates and not selected:
            raise ValueError("请至少勾选一个候选节点")
        config = copy.deepcopy(self.config_data)
        config.test_urls = list(dict.fromkeys(test_urls))
        config.interval_seconds = interval
        config.failure_threshold = threshold
        config.advanced_web_probe = self.advanced_probe_var.get()
        hosts = normalize_allowed_hosts(config.test_urls, self.allowed_hosts_entry.get())
        if config.advanced_web_probe and not hosts:
            raise ValueError("启用高级判定时，请至少允许一个最终重定向域名")
        config.allowed_redirect_hosts = hosts
        config.blocked_url_keywords = [
            item.strip().lower() for item in self.blocked_keywords_entry.get().split(",") if item.strip()
        ]
        config.group = self.group_menu.get()
        if selected:
            config.candidates = selected
        return config

    def save_config(self, notify: bool = True) -> MonitorConfig:
        self.config_data = self._collect_config(require_candidates=True)
        self.config_data.save()
        if notify:
            messagebox.showinfo("已保存", f"已保存 {len(selected)} 个候选节点")
        return self.config_data

    def test_once(self) -> None:
        if self._is_running():
            messagebox.showwarning("服务运行中", "请先停止后台服务，再执行整组测试")
            return
        try:
            config = self._collect_config(require_candidates=False)
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        candidates = list(getattr(self, "_all_candidates", []))
        if not candidates:
            messagebox.showwarning("没有节点", "当前策略组没有可测试的真实节点")
            return
        self.test_button.configure(state="disabled", text=f"测试中 0/{len(candidates)}")
        for item in candidates:
            self.node_test_results[item["name"]] = {"status": "testing"}
        self._render_candidates()

        completed = 0
        def report(name, result):
            nonlocal completed
            if result.get("final", True):
                completed += 1
            current_count = completed
            def update():
                self.node_test_results[name] = result
                label = self.node_status_labels.get(name)
                if label:
                    if result["status"] == "ok":
                        label.configure(text=f"{result['delay']} ms", text_color="#16a34a")
                    else:
                        label.configure(text="失败", text_color="#dc2626")
                self.test_button.configure(text=f"测试中 {current_count}/{len(candidates)}")
            self.after(0, update)

        def worker():
            try:
                results = test_group_candidates(self._client(), config, candidates, report)
                success_count = sum(item["status"] == "ok" for item in results.values())
                self.after(0, lambda: messagebox.showinfo("测试完成", f"成功 {success_count} 个，失败 {len(results) - success_count} 个"))
            except Exception as exc:
                error_text = str(exc)
                self.after(0, lambda text=error_text: messagebox.showerror("测试失败", text))
            finally:
                self.after(0, lambda: self.test_button.configure(state="normal", text="立即测试"))
        threading.Thread(target=worker, daemon=True).start()

    def _is_running(self) -> bool:
        if self.process and self.process.poll() is None:
            return True
        if not PID_PATH.exists():
            return False
        try:
            pid = int(PID_PATH.read_text(encoding="ascii"))
            os.kill(pid, 0)
            return True
        except (ValueError, OSError):
            PID_PATH.unlink(missing_ok=True)
            return False

    def toggle_service(self) -> None:
        if self._is_running():
            self.stop_service()
            return
        try:
            self.save_config(notify=False)
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen([sys.executable, str(ROOT / "monitor_service.py")], cwd=ROOT, creationflags=flags)
        self.after(500, self.refresh_status)

    def stop_service(self) -> None:
        try:
            pid = int(PID_PATH.read_text(encoding="ascii"))
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, check=False)
            else:
                os.kill(pid, signal.SIGTERM)
        except (FileNotFoundError, ValueError, ProcessLookupError):
            pass
        PID_PATH.unlink(missing_ok=True)
        self.process = None
        self.refresh_status()

    def refresh_status(self) -> None:
        running = self._is_running()
        self.service_button.configure(text="停止服务" if running else "启动服务")
        self.status_label.configure(text="服务运行中" if running else "服务未运行")
        if LOG_PATH.exists():
            lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-6:]
            self.log_box.delete("1.0", "end")
            self.log_box.insert("end", "\n".join(lines))
        self.after(1500, self.refresh_status)


def main() -> None:
    ConfigApp().mainloop()


if __name__ == "__main__":
    main()
