from __future__ import annotations

import json
import copy
import os
import pathlib
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.parse
from tkinter import font as tkfont, messagebox

import customtkinter as ctk

from mihomo_client import ControllerSettings, MihomoClient, MihomoError
from monitor_service import (
    CONFIG_PATH,
    LOG_PATH,
    MonitorConfig,
    _ai_studio_page_error,
    cache_node_result,
    probe_nodes_delays,
    resolve_route_group,
    resolve_runtime_group,
    switch_to_best_verified_candidate,
)
from service_manager import MonitorServiceManager, ServiceManagerError


ROOT = pathlib.Path(__file__).resolve().parent


def configure_linux_font(root: tk.Misc) -> None:
    """让 Linux 控件继承桌面环境的默认字体。"""
    if not sys.platform.startswith("linux"):
        return
    default_font = tkfont.nametofont("TkDefaultFont", root=root)
    ctk.ThemeManager.theme["CTkFont"]["family"] = default_font.cget("family")


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


def filter_current_node_logs(lines: list[str], current: str, limit: int = 40) -> list[str]:
    """只保留日志中的当前节点探测结果。"""
    if not current:
        return []
    markers = (
        f"节点可用：{current}，",
        f"节点不可用：{current}（",
        f"当前节点探测失败（",
    )
    filtered = []
    for line in lines:
        if markers[0] in line or markers[1] in line:
            filtered.append(line)
        elif markers[2] in line and f"：{current}（" in line:
            filtered.append(line)
    return filtered[-limit:]


def test_group_candidates(client, config: MonitorConfig, candidates, on_result=None):
    """并行测试策略组全部候选，测试期间不切换节点。"""
    proxies = client.proxies()
    groups = {name: item for name, item in proxies.items() if item.get("type") == "Selector"}
    route_group = resolve_route_group(client, config, groups, proxies)
    runtime_group = route_group or resolve_runtime_group(config, groups, proxies)
    config.group = runtime_group
    _, live_candidates = client.selector_group_snapshot(runtime_group, proxies=proxies)
    live_names = [item["name"] for item in live_candidates]
    live_name_set = set(live_names)
    requested_names = [item["name"] if isinstance(item, dict) else str(item) for item in candidates]
    names = [name for name in requested_names if name in live_name_set] or live_names
    if not names:
        raise ValueError(f"策略组没有可测试的真实节点：{runtime_group}")

    def report(name, result):
        result["final"] = True
        if on_result:
            on_result(name, result)

    return probe_nodes_delays(client, config, names, report)


class ConfigApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Clash Verge 节点守护")
        self.geometry("920x1000")
        self.minsize(780, 900)
        ctk.set_appearance_mode("system")
        ctk.set_default_color_theme("blue")
        configure_linux_font(self)
        self.config_data = MonitorConfig.load()
        self.candidate_vars: dict[str, tk.BooleanVar] = {}
        self.node_status_labels = {}
        self.node_test_results = {}
        self.selected_candidates = set(self.config_data.candidates)
        self._live_candidate_names: set[str] = set()
        self._candidate_group = ""
        self._group_live_candidate_names: dict[str, set[str]] = {}
        # 勾选状态按节点名称全局保存；同名节点出现在多个策略组时，所有视图共享一个状态。
        self._group_selected_candidates: dict[str, set[str]] = {}
        if self.config_data.group:
            self._group_selected_candidates[self.config_data.group] = set(self.config_data.candidates)
        self.provider_filter = "全部订阅"
        self._selection_anchor: str | None = None
        self._shift_pressed = False
        self._visible_candidate_names: list[str] = []
        self.process: subprocess.Popen[str] | None = None
        self.service_manager = MonitorServiceManager(ROOT)
        self._current_node_refreshing = False
        self.current_node_name = ""
        self._nodes_window = None
        self.nodes_frame = None
        self.provider_menu = None
        self.test_button = None
        self.node_name_widgets = {}
        self.node_test_cache: dict[str, dict[str, object]] = {}
        self._manual_round_id = 0
        self._manual_task_running = False
        self._manual_log_entries: list[tuple[str, str]] = []
        self._last_log_text = ""
        self._status_refresh_job: str | None = None
        self.bind_all("<KeyPress-Shift_L>", self._mark_shift_pressed, add="+")
        self.bind_all("<KeyPress-Shift_R>", self._mark_shift_pressed, add="+")
        self.bind_all("<KeyRelease-Shift_L>", self._mark_shift_released, add="+")
        self.bind_all("<KeyRelease-Shift_R>", self._mark_shift_released, add="+")
        self._build()
        self.after(200, self.refresh_groups)
        self._schedule_status_refresh(1000)

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=0)
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
        ctk.CTkLabel(settings, text="失败阈值（默认 1）").grid(row=1, column=2, padx=8, pady=10)
        self.threshold_entry = ctk.CTkEntry(settings, width=80)
        self.threshold_entry.insert(0, str(self.config_data.failure_threshold))
        self.threshold_entry.grid(row=1, column=3, padx=(0, 14), pady=10)

        self.advanced_probe_var = tk.BooleanVar(value=self.config_data.advanced_web_probe)
        self.advanced_probe_check = ctk.CTkCheckBox(
            settings,
            text="启用严格状态判定（含 AI Studio 地区跳转）",
            variable=self.advanced_probe_var,
        )
        self.advanced_probe_check.grid(row=2, column=0, columnspan=4, padx=14, pady=(6, 12), sticky="w")

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=2, column=0, padx=24, pady=14, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        controls = ctk.CTkFrame(content, fg_color="transparent")
        controls.grid(row=0, column=0, sticky="ew")
        controls.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(controls, text="待切换策略组").grid(row=0, column=0, padx=(0, 8))
        self.group_menu = ctk.CTkOptionMenu(controls, values=["正在读取…"], command=lambda _: self.refresh_candidates())
        self.group_menu.grid(row=0, column=1, sticky="ew")
        self.refresh_button = ctk.CTkButton(controls, text="刷新", width=80, command=self.refresh_groups)
        self.refresh_button.grid(row=0, column=2, padx=(8, 0))

        current_frame = ctk.CTkFrame(content, fg_color="transparent")
        current_frame.grid(row=1, column=0, pady=(14, 4), sticky="ew")
        current_frame.grid_columnconfigure(0, weight=1)
        self.current_node_label = ctk.CTkLabel(current_frame, text="当前使用节点：正在读取…", anchor="w")
        self.current_node_label.grid(row=0, column=0, sticky="w")
        self.test_current_button = ctk.CTkButton(
            current_frame,
            text="测试当前节点",
            width=120,
            command=self.test_current_node,
        )
        self.test_current_button.grid(row=0, column=1, padx=(10, 0))
        self.switch_button = ctk.CTkButton(
            current_frame,
            text="立即切换",
            width=100,
            command=self.switch_now,
        )
        self.switch_button.grid(row=0, column=2, padx=(8, 0))

        candidate_bar = ctk.CTkFrame(content, fg_color="transparent")
        candidate_bar.grid(row=2, column=0, pady=(8, 0), sticky="ew")
        candidate_bar.grid_columnconfigure(0, weight=1)
        self.candidate_summary_label = ctk.CTkLabel(
            candidate_bar,
            text="候选节点（仅从当前运行配置抽取）",
            anchor="w",
        )
        self.candidate_summary_label.grid(row=0, column=0, sticky="w")
        self.view_nodes_button = ctk.CTkButton(
            candidate_bar,
            text="查看节点",
            width=100,
            command=self.show_nodes_panel,
        )
        self.view_nodes_button.grid(row=0, column=1, padx=(10, 0))

        footer = ctk.CTkFrame(self, corner_radius=6)
        footer.grid(row=3, column=0, padx=24, pady=(0, 20), sticky="ew")
        footer.grid_columnconfigure(2, weight=1)
        self.save_button = ctk.CTkButton(footer, text="保存配置", command=self.save_config)
        self.save_button.grid(row=0, column=0, padx=10, pady=10)
        self.service_button = ctk.CTkButton(
            footer,
            text="启动服务",
            fg_color="#16a34a",
            hover_color="#15803d",
            command=self.toggle_service,
        )
        self.service_button.grid(row=0, column=1, padx=10, pady=10)
        self.status_label = ctk.CTkLabel(footer, text="服务未运行")
        self.status_label.grid(row=0, column=2, padx=10, pady=10, sticky="e")
        self.log_box = ctk.CTkTextbox(footer, height=400)
        self.log_box.grid(row=1, column=0, columnspan=3, padx=10, pady=(0, 10), sticky="ew")

    def show_nodes_panel(self) -> None:
        """显示候选节点弹出面板。"""
        if self._nodes_window is not None and self._nodes_window.winfo_exists():
            self._nodes_window.deiconify()
            self._nodes_window.lift()
            self._nodes_window.focus_force()
            self._render_candidates()
            return

        window = ctk.CTkToplevel(self)
        window.title("候选节点")
        window.geometry("760x650")
        window.minsize(620, 420)
        window.transient(self)
        window.grid_columnconfigure(0, weight=1)
        window.grid_rowconfigure(1, weight=1)
        window.protocol("WM_DELETE_WINDOW", window.withdraw)
        self._nodes_window = window

        filters = ctk.CTkFrame(window, fg_color="transparent")
        filters.grid(row=0, column=0, padx=12, pady=(12, 6), sticky="ew")
        filters.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(filters, text="订阅来源").grid(row=0, column=0, padx=(0, 8))
        self.provider_menu = ctk.CTkOptionMenu(filters, values=["全部订阅"], command=self._filter_provider)
        self.provider_menu.grid(row=0, column=1, sticky="ew")
        self.select_all_button = ctk.CTkButton(filters, text="全选/反选", width=90, command=self.toggle_all)
        self.select_all_button.grid(row=0, column=2, padx=(8, 0))

        self.nodes_frame = ctk.CTkScrollableFrame(window, corner_radius=6)
        self.nodes_frame.grid(row=1, column=0, padx=12, pady=6, sticky="nsew")
        self.nodes_frame.grid_columnconfigure(0, weight=1)
        if sys.platform.startswith("linux"):
            self.bind_all("<Button-4>", lambda event: self._scroll_candidates(event, -3), add="+")
            self.bind_all("<Button-5>", lambda event: self._scroll_candidates(event, 3), add="+")

        panel_footer = ctk.CTkFrame(window, fg_color="transparent")
        panel_footer.grid(row=2, column=0, padx=12, pady=(6, 12), sticky="ew")
        panel_footer.grid_columnconfigure(1, weight=1)
        self.test_button = ctk.CTkButton(panel_footer, text="立即测试", command=self.test_once)
        self.test_button.grid(row=0, column=0, sticky="w")
        self.panel_hint_label = ctk.CTkLabel(panel_footer, text="测试不会切换当前节点", text_color="gray60")
        self.panel_hint_label.grid(row=0, column=1, padx=(12, 0), sticky="w")
        self._configure_provider_menu()
        self._render_candidates()

    def _client(self) -> MihomoClient:
        return MihomoClient(
            ControllerSettings(
                url=self.config_data.controller_url,
                socket_path=self.config_data.controller_socket,
                pipe_path=self.config_data.controller_pipe,
            )
        )

    def _background(self, func, success=None, failure=None) -> None:
        def worker() -> None:
            try:
                result = func()
                if success:
                    self.after(0, lambda: success(result))
            except Exception as exc:
                if failure:
                    self.after(0, lambda: failure(exc))
                else:
                    self.after(0, lambda: messagebox.showerror("操作失败", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _set_current_node(self, current: str, text: str | None = None) -> None:
        """更新当前节点名称，并刷新按节点过滤的日志。"""
        self.current_node_name = current
        label_text = text if text is not None else f"当前使用节点：{current or '未选择'}"
        self.current_node_label.configure(text=label_text)
        self._refresh_log_box()

    def _configure_provider_menu(self, providers: list[str] | None = None) -> None:
        if self.provider_menu is None or not self.provider_menu.winfo_exists():
            return
        values = providers or ["全部订阅"] + sorted(
            {item["provider"] for item in getattr(self, "_all_candidates", [])}
        )
        self.provider_menu.configure(values=values)
        if self.provider_filter not in values:
            self.provider_filter = "全部订阅"
        self.provider_menu.set(self.provider_filter)

    def _update_candidate_summary(self) -> None:
        count = len(getattr(self, "_all_candidates", []))
        self.candidate_summary_label.configure(
            text=f"候选节点（{count} 个，仅从当前运行配置抽取）"
        )

    def refresh_groups(self) -> None:
        self.refresh_button.configure(state="disabled")
        def done(snapshot: tuple[dict, dict, str | None]) -> None:
            groups, proxies, route_group = snapshot
            names = list(groups)
            if not names:
                names = ["没有 Selector 策略组"]
                selected = names[0]
            else:
                try:
                    configured_group = groups.get(self.config_data.group, {})
                    configured_names = {
                        str(item)
                        for item in configured_group.get("all", [])
                        if str(item) in proxies and proxies[str(item)].get("type") not in {
                            "Selector",
                            "URLTest",
                            "Fallback",
                            "LoadBalance",
                            "Direct",
                            "Reject",
                            "Pass",
                            "Compatible",
                        }
                    }
                    prefer_largest = bool(self.config_data.candidates) and not set(
                        self.config_data.candidates
                    ).issubset(configured_names)
                    selected = route_group
                    if not selected:
                        selected = resolve_runtime_group(
                            self.config_data,
                            groups,
                            proxies,
                            prefer_largest=prefer_largest,
                        )
                except MihomoError:
                    names.insert(0, "请选择有效策略组")
                    selected = names[0]
            self.group_menu.configure(values=names)
            self.group_menu.set(selected)
            current = str(groups.get(selected, {}).get("now") or "")
            self._set_current_node(current)
            self.refresh_button.configure(state="normal")
            self.refresh_candidates()
        def load_groups() -> tuple[dict, dict, str | None]:
            client = self._client()
            proxies = client.proxies()
            groups = {name: item for name, item in proxies.items() if item.get("type") == "Selector"}
            route_group = resolve_route_group(client, self.config_data, groups, proxies)
            return groups, proxies, route_group

        self._background(load_groups, done)

    def refresh_candidates(self) -> None:
        self._capture_visible_selection()
        group = self.group_menu.get()
        self.node_test_results = {}
        self.node_test_cache = {}
        self._manual_log_entries = []
        self._selection_anchor = None
        if group in {"没有 Selector 策略组", "请选择有效策略组"}:
            self._set_current_node("", "当前使用节点：没有可用策略组")
            self._all_candidates = []
            self.provider_filter = "全部订阅"
            self._update_candidate_summary()
            self._configure_provider_menu([self.provider_filter])
            self._render_candidates()
            return
        self._set_current_node("", "当前使用节点：正在读取…")
        def done(snapshot: tuple[str, list[dict[str, str]]]) -> None:
            current, items = snapshot
            if group != self.group_menu.get():
                return
            live_names = {item["name"] for item in items}
            previous_live = self._group_live_candidate_names.get(group)
            if previous_live is None:
                # 首次进入分组时沿用其它分组已经勾选的同名节点；完全没有交集才默认全选。
                if not self.selected_candidates.intersection(live_names):
                    self.selected_candidates.update(live_names)
            elif previous_live and not previous_live.intersection(live_names):
                # 订阅整体替换时，当前组的新节点全部进入候选，同时保留其它分组的选择。
                self.selected_candidates.update(live_names)
            self._live_candidate_names = live_names
            self._group_live_candidate_names[group] = set(live_names)
            self._sync_group_selection_maps()
            self._candidate_group = group
            self._set_current_node(current)
            self._all_candidates = items
            providers = ["全部订阅"] + sorted({item["provider"] for item in items})
            self._update_candidate_summary()
            self._configure_provider_menu(providers)
            self._render_candidates()
        self._background(lambda: self._client().selector_group_snapshot(group), done)

    def _sync_group_selection_maps(self) -> None:
        """把全局勾选集合投影到各已读取策略组，供状态保持和调试使用。"""
        self._group_selected_candidates = {
            group: self.selected_candidates.intersection(names)
            for group, names in self._group_live_candidate_names.items()
        }

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
        self._sync_group_selection_maps()

    def _mark_shift_pressed(self, _event: tk.Event) -> None:
        self._shift_pressed = True

    def _mark_shift_released(self, _event: tk.Event) -> None:
        self._shift_pressed = False

    def _toggle_candidate(self, name: str, var: tk.BooleanVar) -> None:
        if self._shift_pressed and self._selection_anchor in self.candidate_vars:
            start = self._visible_candidate_names.index(self._selection_anchor)
            end = self._visible_candidate_names.index(name)
            for candidate_name in self._visible_candidate_names[min(start, end):max(start, end) + 1]:
                self.candidate_vars[candidate_name].set(var.get())
        else:
            self._selection_anchor = name
        self._capture_visible_selection()

    def _scroll_candidates(self, event: tk.Event, units: int) -> str | None:
        if self.nodes_frame is None:
            return None
        if self.nodes_frame.check_if_master_is_canvas(event.widget):
            self.nodes_frame._parent_canvas.yview_scroll(units, "units")
            return "break"
        return None

    def _render_candidates(self) -> None:
        if self.nodes_frame is None or not self.nodes_frame.winfo_exists():
            return
        for child in self.nodes_frame.winfo_children():
            child.destroy()
        self.candidate_vars = {}
        self.node_status_labels = {}
        self.node_name_widgets = {}
        shown = [item for item in getattr(self, "_all_candidates", []) if self.provider_filter == "全部订阅" or item["provider"] == self.provider_filter]
        self._visible_candidate_names = [item["name"] for item in shown]
        if self._selection_anchor not in self._visible_candidate_names:
            self._selection_anchor = None
        for row, item in enumerate(shown):
            var = tk.BooleanVar(value=item["name"] in self.selected_candidates)
            self.candidate_vars[item["name"]] = var
            result = self.node_test_results.get(item["name"])
            status_text = ""
            status_color = "gray60"
            if result and result["status"] == "testing":
                status_text = "测试中"
                status_color = "#d97706"
            elif result and result["status"] == "ok":
                status_text = f"{result['delay']} ms"
                status_color = "#16a34a"
            elif result:
                status_text = "失败"
                status_color = "#dc2626"
            box_kwargs = {
                "text": item["name"],
                "variable": var,
                "command": lambda name=item["name"], candidate_var=var: self._toggle_candidate(name, candidate_var),
            }
            if result:
                box_kwargs["text_color"] = status_color
            box = ctk.CTkCheckBox(
                self.nodes_frame,
                **box_kwargs,
            )
            box.grid(row=row, column=0, padx=10, pady=5, sticky="w")
            self.node_name_widgets[item["name"]] = box
            status = ctk.CTkLabel(self.nodes_frame, text=status_text, text_color=status_color, width=72, anchor="e")
            status.grid(row=row, column=1, padx=(6, 2), pady=5, sticky="e")
            self.node_status_labels[item["name"]] = status
            ctk.CTkLabel(self.nodes_frame, text=item["provider"], text_color="gray60").grid(row=row, column=2, padx=(6, 10), pady=5, sticky="e")

    def toggle_all(self) -> None:
        target = not all(var.get() for var in self.candidate_vars.values())
        for var in self.candidate_vars.values():
            var.set(target)
        self._selection_anchor = None
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
        config.group = self.group_menu.get()
        if selected:
            config.candidates = selected
        return config

    def save_config(self, notify: bool = True) -> MonitorConfig:
        self.config_data = self._collect_config(require_candidates=True)
        self.config_data.save()
        if notify:
            messagebox.showinfo("已保存", f"已保存 {len(self.config_data.candidates)} 个候选节点")
        return self.config_data

    def _set_manual_buttons(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in (
            self.test_current_button,
            self.switch_button,
            self.view_nodes_button,
            self.test_button,
        ):
            if button is not None and button.winfo_exists():
                button.configure(state=state)

    def _finish_manual_task(self) -> None:
        self._manual_task_running = False
        unfinished = [
            name for name, result in self.node_test_results.items()
            if result.get("status") == "testing"
        ]
        for name in unfinished:
            self.node_test_results.pop(name, None)
        if unfinished:
            self._render_candidates()
        self._set_manual_buttons(True)
        if self.test_button is not None and self.test_button.winfo_exists():
            self.test_button.configure(text="立即测试")
        if self.test_current_button.winfo_exists():
            self.test_current_button.configure(text="测试当前节点")
        if self.switch_button.winfo_exists():
            self.switch_button.configure(text="立即切换")

    def _next_manual_round(self) -> int:
        self._manual_round_id += 1
        return self._manual_round_id

    def _apply_node_result(self, name: str, result: dict[str, object]) -> None:
        self.node_test_results[name] = result
        label = self.node_status_labels.get(name)
        box = self.node_name_widgets.get(name)
        if result.get("status") == "ok":
            status_text = f"{result['delay']} ms"
            status_color = "#16a34a"
        elif result.get("status") == "testing":
            status_text = "测试中"
            status_color = "#d97706"
        else:
            status_text = "失败"
            status_color = "#dc2626"
        if label:
            label.configure(text=status_text, text_color=status_color)
        if box:
            box.configure(text_color=status_color)

    def _append_manual_log(self, node: str, message: str) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self._manual_log_entries.append((node, f"{timestamp} [INFO] {message}"))
        self._manual_log_entries = self._manual_log_entries[-100:]
        self._refresh_log_box()

    def _refresh_log_box(self) -> None:
        if not hasattr(self, "log_box") or not self.log_box.winfo_exists():
            return
        lines = []
        if LOG_PATH.exists():
            try:
                lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                lines = []
        filtered = filter_current_node_logs(lines, self.current_node_name)
        filtered.extend(
            line for node, line in self._manual_log_entries if node == self.current_node_name
        )
        text = "\n".join(filtered[-40:])
        if text == self._last_log_text:
            return
        self._last_log_text = text
        self.log_box.delete("1.0", "end")
        if text:
            self.log_box.insert("end", text)

    def test_once(self) -> None:
        if self._manual_task_running:
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
        self._manual_task_running = True
        self._set_manual_buttons(False)
        self.test_button.configure(state="disabled", text=f"测试中 0/{len(candidates)}")
        for item in candidates:
            self.node_test_results[item["name"]] = {"status": "testing"}
        self._render_candidates()

        completed = 0
        round_id = self._next_manual_round()

        def report(name, result):
            nonlocal completed
            if result.get("final", True):
                completed += 1
            current_count = completed

            if result.get("final", True):
                cache_node_result(self.node_test_cache, name, result, round_id)

            def update():
                self._apply_node_result(name, result)
                if self.test_button is not None and self.test_button.winfo_exists():
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
                self.after(0, self._finish_manual_task)
        threading.Thread(target=worker, daemon=True).start()

    def test_current_node(self) -> None:
        if self._manual_task_running:
            return
        try:
            config = self._collect_config(require_candidates=False)
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return

        self._manual_task_running = True
        self._set_manual_buttons(False)
        self.test_current_button.configure(text="测试中…")
        round_id = self._next_manual_round()

        def worker():
            try:
                client = self._client()
                groups = client.selector_groups()
                proxies = client.proxies()
                route_group = resolve_route_group(client, config, groups, proxies)
                runtime_group = route_group or resolve_runtime_group(config, groups, proxies)
                current, _ = client.selector_group_snapshot(runtime_group, proxies=proxies)
                if not current:
                    raise ValueError("当前策略组没有选中节点")
                config.group = runtime_group
                result = probe_nodes_delays(client, config, [current])[current]
                if result["status"] == "ok":
                    page_error = _ai_studio_page_error(client, config)
                    if page_error:
                        result["status"] = "failed"
                        result["error"] = page_error
                result["final"] = True
                cache_node_result(self.node_test_cache, current, result, round_id)

                def done():
                    if runtime_group == self.group_menu.get():
                        self._set_current_node(current)
                    self._apply_node_result(current, result)
                    if result["status"] == "ok":
                        self._append_manual_log(
                            current,
                            f"手动测试节点可用：{current}，最大延迟 {result['delay']} ms，明细={result['delays']}",
                        )
                        messagebox.showinfo("测试完成", f"当前节点可用，最大延迟 {result['delay']} ms")
                    else:
                        error = result.get("error", "未知错误")
                        self._append_manual_log(current, f"手动测试节点不可用：{current}（{error}）")
                        messagebox.showwarning("测试完成", f"当前节点不可用：{error}")

                self.after(0, done)
            except Exception as exc:
                error_text = str(exc)
                self.after(0, lambda text=error_text: messagebox.showerror("测试失败", text))
            finally:
                self.after(0, self._finish_manual_task)

        threading.Thread(target=worker, daemon=True).start()

    def switch_now(self) -> None:
        if self._is_running():
            messagebox.showwarning("服务运行中", "请先停止后台服务，再执行手动切换")
            return
        if self._manual_task_running:
            return
        try:
            config = self._collect_config(require_candidates=False)
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        available_names = {item["name"] for item in getattr(self, "_all_candidates", [])}
        candidate_names = [
            name for name in config.candidates if name in available_names
        ]
        if not candidate_names:
            messagebox.showwarning("没有候选节点", "请先在查看节点面板中选择候选节点")
            return

        self._manual_task_running = True
        self._set_manual_buttons(False)
        self.switch_button.configure(text="切换中…")
        for name in candidate_names:
            self.node_test_results[name] = {"status": "testing"}
        self._render_candidates()

        def worker():
            selected_result = None
            try:
                client = self._client()
                proxies = client.proxies()
                groups = {name: item for name, item in proxies.items() if item.get("type") == "Selector"}
                route_group = resolve_route_group(client, config, groups, proxies)
                runtime_group = route_group or resolve_runtime_group(config, groups, proxies)
                config.group = runtime_group
                current, _ = client.selector_group_snapshot(runtime_group, proxies=proxies)
                if not current:
                    raise ValueError("当前策略组没有选中节点")
                cached = switch_to_best_verified_candidate(
                    client,
                    config,
                    current,
                    self.node_test_cache,
                )
                if cached:
                    selected_result = cached
                    self.after(0, lambda: self._handle_manual_switch(config.group, current, cached))
                    return

                config.candidates = candidate_names
                round_id = self._next_manual_round()

                def report(name, result):
                    nonlocal selected_result
                    result = dict(result)
                    cache_node_result(self.node_test_cache, name, result, round_id)
                    self.after(0, lambda node=name, item=result: self._apply_node_result(node, item))
                    if result.get("status") == "ok" and selected_result is None:
                        switched = switch_to_best_verified_candidate(
                            client,
                            config,
                            current,
                            self.node_test_cache,
                            round_id,
                        )
                        if switched:
                            selected_result = switched
                            self.after(
                                0,
                                lambda item=switched: self._handle_manual_switch(
                                    config.group,
                                    current,
                                    item,
                                ),
                            )

                results = probe_nodes_delays(client, config, candidate_names, report)
                if selected_result is None:
                    self.after(
                        0,
                        lambda: messagebox.showwarning(
                            "切换失败",
                            "当前没有可用的候选节点，保持当前节点不变",
                        ),
                    )
            except Exception as exc:
                error_text = str(exc)
                self.after(0, lambda text=error_text: messagebox.showerror("切换失败", text))
            finally:
                self.after(0, self._finish_manual_task)

        threading.Thread(target=worker, daemon=True).start()

    def _handle_manual_switch(self, group: str, previous: str, result: dict[str, object]) -> None:
        selected = str(result["selected"])
        if self.group_menu.get() == group:
            self._set_current_node(selected)
        self._apply_node_result(selected, {"status": "ok", "delay": result["delay"]})
        self._append_manual_log(
            selected,
            f"手动切换节点：{previous} -> {selected}（{result['delay']} ms，{result['cache_source']}缓存）",
        )
        if self.switch_button.winfo_exists():
            self.switch_button.configure(text="已切换")

    def _is_running(self) -> bool:
        return self.service_manager.is_running()

    def toggle_service(self) -> None:
        if self._is_running():
            self.stop_service()
            return
        try:
            self.save_config(notify=False)
        except Exception as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        try:
            self.process = self.service_manager.start()
        except ServiceManagerError as exc:
            messagebox.showerror("启动服务失败", str(exc))
            return
        self._schedule_status_refresh(500)

    def stop_service(self) -> None:
        try:
            self.service_manager.stop()
        except ServiceManagerError as exc:
            messagebox.showerror("停止服务失败", str(exc))
            return
        self.process = None
        self.refresh_status()

    def _refresh_current_node(self) -> None:
        group = self.group_menu.get()
        if self._current_node_refreshing or group in {"", "正在读取…", "没有 Selector 策略组"}:
            return
        self._current_node_refreshing = True

        def done(current: str) -> None:
            if group == self.group_menu.get():
                self._set_current_node(current)
            self._current_node_refreshing = False

        def failed(_exc: Exception) -> None:
            if group == self.group_menu.get():
                self._set_current_node("", "当前使用节点：无法读取")
            self._current_node_refreshing = False

        def read_current() -> str:
            client = self._client()
            proxies = client.proxies()
            return client.selector_group_snapshot(group, proxies=proxies)[0]

        self._background(read_current, done, failed)

    def refresh_status(self) -> None:
        running = self._is_running()
        if running:
            self.service_button.configure(text="停止服务", fg_color="#dc2626", hover_color="#b91c1c")
        else:
            self.service_button.configure(text="启动服务", fg_color="#16a34a", hover_color="#15803d")
        self.status_label.configure(text="服务运行中" if running else "服务未运行")
        self._refresh_current_node()
        self._refresh_log_box()
        self._schedule_status_refresh()

    def _schedule_status_refresh(self, delay_ms: int = 3000) -> None:
        """确保状态刷新始终只有一个待执行的定时回调。"""
        if self._status_refresh_job is not None:
            try:
                self.after_cancel(self._status_refresh_job)
            except tk.TclError:
                pass
        self._status_refresh_job = self.after(delay_ms, self._run_status_refresh)

    def _run_status_refresh(self) -> None:
        self._status_refresh_job = None
        self.refresh_status()


def main() -> None:
    ConfigApp().mainloop()


if __name__ == "__main__":
    main()
