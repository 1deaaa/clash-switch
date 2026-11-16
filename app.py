from __future__ import annotations

import copy
import os
import pathlib
import subprocess
import sys
import threading
import time
import urllib.parse

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent, QColor, QFont, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

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
GRAY_TEXT = "#9ca3af"


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
        f"-> {current}",
        f"切换到 {current}",
    )
    filtered = []
    for line in lines:
        if markers[0] in line or markers[1] in line:
            filtered.append(line)
        elif markers[2] in line and f"：{current}（" in line:
            filtered.append(line)
        elif markers[3] in line or markers[4] in line:
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


class _UiBus(QObject):
    """把后台线程的回调排队到 UI 线程执行。"""

    call = Signal(object)


def _apply_palette(app: QApplication) -> None:
    if app.styleHints().colorScheme() != Qt.ColorScheme.Dark:
        return
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(43, 43, 43))
    palette.setColor(QPalette.WindowText, QColor(220, 220, 220))
    palette.setColor(QPalette.Base, QColor(32, 32, 32))
    palette.setColor(QPalette.AlternateBase, QColor(43, 43, 43))
    palette.setColor(QPalette.Text, QColor(220, 220, 220))
    palette.setColor(QPalette.Button, QColor(55, 55, 55))
    palette.setColor(QPalette.ButtonText, QColor(220, 220, 220))
    palette.setColor(QPalette.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    palette.setColor(QPalette.ToolTipBase, QColor(43, 43, 43))
    palette.setColor(QPalette.ToolTipText, QColor(220, 220, 220))
    palette.setColor(QPalette.PlaceholderText, QColor(160, 160, 160))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(120, 120, 120))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(120, 120, 120))
    app.setPalette(palette)


class ConfigApp(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Clash Verge 节点守护")
        self.resize(920, 1000)
        self.setMinimumSize(780, 900)
        self._bus = _UiBus()
        self._bus.call.connect(lambda fn: fn())
        self._closing = False
        self.config_data = MonitorConfig.load()
        self.candidate_vars: dict[str, QCheckBox] = {}
        self.node_status_labels: dict[str, QLabel] = {}
        self.node_test_results: dict[str, dict[str, object]] = {}
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
        self._visible_candidate_names: list[str] = []
        self.process: subprocess.Popen[str] | None = None
        self.service_manager = MonitorServiceManager(ROOT)
        self._current_node_refreshing = False
        self.current_node_name = ""
        self._nodes_window: QDialog | None = None
        self.nodes_frame: QWidget | None = None
        self.nodes_layout: QGridLayout | None = None
        self.provider_menu: QComboBox | None = None
        self.test_button: QPushButton | None = None
        self.node_name_widgets: dict[str, QCheckBox] = {}
        self.node_test_cache: dict[str, dict[str, object]] = {}
        self._manual_round_id = 0
        self._manual_task_running = False
        self._manual_log_entries: list[tuple[str, str]] = []
        self._last_log_text = ""
        self._groups_request_id = 0
        self._candidates_request_id = 0
        self._known_group_names: set[str] = set()
        self._all_candidates: list[dict[str, str]] = []
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._run_status_refresh)
        self._build()
        QTimer.singleShot(200, self.refresh_groups)
        self._schedule_status_refresh(1000)

    def _build(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(24, 20, 24, 20)
        root_layout.setSpacing(14)

        title = QLabel("Clash Verge 节点守护")
        title_font = QFont(self.font())
        title_font.setPointSize(18)
        title_font.setBold(True)
        title.setFont(title_font)
        root_layout.addWidget(title)

        settings = QFrame()
        settings.setFrameShape(QFrame.StyledPanel)
        settings_grid = QGridLayout(settings)
        settings_grid.setContentsMargins(14, 10, 14, 10)
        settings_grid.setHorizontalSpacing(10)
        settings_grid.setColumnStretch(1, 1)
        settings_grid.addWidget(QLabel("测试网址"), 0, 0)
        self.url_entry = QLineEdit(", ".join(self.config_data.test_urls))
        settings_grid.addWidget(self.url_entry, 0, 1, 1, 3)
        settings_grid.addWidget(QLabel("间隔（秒）"), 1, 0)
        self.interval_entry = QLineEdit(str(self.config_data.interval_seconds))
        self.interval_entry.setFixedWidth(100)
        settings_grid.addWidget(self.interval_entry, 1, 1)
        settings_grid.addWidget(QLabel("失败阈值（默认 1）"), 1, 2)
        self.threshold_entry = QLineEdit(str(self.config_data.failure_threshold))
        self.threshold_entry.setFixedWidth(80)
        settings_grid.addWidget(self.threshold_entry, 1, 3)
        self.advanced_probe_check = QCheckBox("启用严格状态判定（含 AI Studio 地区跳转）")
        self.advanced_probe_check.setChecked(self.config_data.advanced_web_probe)
        settings_grid.addWidget(self.advanced_probe_check, 2, 0, 1, 4)
        root_layout.addWidget(settings)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("待切换策略组"))
        self.group_menu = QComboBox()
        self.group_menu.addItem("正在读取…")
        self.group_menu.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.group_menu.currentIndexChanged.connect(lambda _: self.refresh_candidates())
        controls.addWidget(self.group_menu, 1)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setFixedWidth(80)
        self.refresh_button.clicked.connect(self.refresh_groups)
        controls.addWidget(self.refresh_button)
        root_layout.addLayout(controls)

        current_row = QHBoxLayout()
        self.current_node_label = QLabel("当前使用节点：正在读取…")
        self.current_node_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        current_row.addWidget(self.current_node_label, 1)
        self.test_current_button = QPushButton("测试当前节点")
        self.test_current_button.setFixedWidth(120)
        self.test_current_button.clicked.connect(self.test_current_node)
        current_row.addWidget(self.test_current_button)
        self.switch_button = QPushButton("立即切换")
        self.switch_button.setFixedWidth(100)
        self.switch_button.clicked.connect(self.switch_now)
        current_row.addWidget(self.switch_button)
        root_layout.addLayout(current_row)

        candidate_row = QHBoxLayout()
        self.candidate_summary_label = QLabel("候选节点（仅从当前运行配置抽取）")
        self.candidate_summary_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        candidate_row.addWidget(self.candidate_summary_label, 1)
        self.view_nodes_button = QPushButton("查看节点")
        self.view_nodes_button.setFixedWidth(100)
        self.view_nodes_button.clicked.connect(self.show_nodes_panel)
        candidate_row.addWidget(self.view_nodes_button)
        root_layout.addLayout(candidate_row)

        footer = QFrame()
        footer.setFrameShape(QFrame.StyledPanel)
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(10, 10, 10, 10)
        footer_buttons = QHBoxLayout()
        self.save_button = QPushButton("保存配置")
        self.save_button.clicked.connect(lambda: self.save_config())
        footer_buttons.addWidget(self.save_button)
        self.service_button = QPushButton("启动服务")
        self.service_button.setStyleSheet("background-color: #16a34a; color: white;")
        self.service_button.clicked.connect(self.toggle_service)
        footer_buttons.addWidget(self.service_button)
        self.status_label = QLabel("服务未运行")
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        footer_buttons.addWidget(self.status_label, 1)
        footer_layout.addLayout(footer_buttons)
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMinimumHeight(300)
        self.log_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        footer_layout.addWidget(self.log_box, 1)
        root_layout.addWidget(footer, 1)

    def show_nodes_panel(self) -> None:
        """显示候选节点弹出面板。"""
        if self._nodes_window is not None:
            self._nodes_window.show()
            self._nodes_window.raise_()
            self._nodes_window.activateWindow()
            self._render_candidates()
            return

        window = QDialog(self)
        window.setWindowTitle("候选节点")
        window.resize(760, 650)
        window.setMinimumSize(620, 420)
        window.setModal(False)
        layout = QVBoxLayout(window)
        layout.setContentsMargins(12, 12, 12, 12)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("订阅来源"))
        self.provider_menu = QComboBox()
        self.provider_menu.addItem("全部订阅")
        self.provider_menu.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.provider_menu.currentTextChanged.connect(self._filter_provider)
        filters.addWidget(self.provider_menu, 1)
        self.select_all_button = QPushButton("全选/反选")
        self.select_all_button.setFixedWidth(90)
        self.select_all_button.clicked.connect(self.toggle_all)
        filters.addWidget(self.select_all_button)
        layout.addLayout(filters)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.StyledPanel)
        self.nodes_frame = QWidget()
        self.nodes_layout = QGridLayout(self.nodes_frame)
        self.nodes_layout.setContentsMargins(10, 10, 10, 10)
        self.nodes_layout.setColumnStretch(0, 1)
        self.nodes_layout.setAlignment(Qt.AlignTop)
        scroll.setWidget(self.nodes_frame)
        layout.addWidget(scroll, 1)

        panel_footer = QHBoxLayout()
        self.test_button = QPushButton("立即测试")
        self.test_button.clicked.connect(self.test_once)
        panel_footer.addWidget(self.test_button)
        hint = QLabel("测试不会切换当前节点")
        hint.setStyleSheet(f"color: {GRAY_TEXT};")
        panel_footer.addWidget(hint, 1)
        layout.addLayout(panel_footer)

        self._nodes_window = window
        self._configure_provider_menu()
        self._render_candidates()
        window.show()

    def _client(self) -> MihomoClient:
        return MihomoClient(
            ControllerSettings(
                url=self.config_data.controller_url,
                socket_path=self.config_data.controller_socket,
                pipe_path=self.config_data.controller_pipe,
            )
        )

    def _post(self, fn) -> None:
        if self._closing:
            return
        self._bus.call.emit(fn)

    def _background(self, func, success=None, failure=None) -> None:
        def worker() -> None:
            try:
                result = func()
                if success:
                    self._post(lambda: success(result))
            except Exception as exc:
                if failure:
                    self._post(lambda: failure(exc))
                else:
                    self._post(lambda: QMessageBox.critical(self, "操作失败", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _set_current_node(self, current: str, text: str | None = None) -> None:
        """更新当前节点名称，并刷新按节点过滤的日志。"""
        self.current_node_name = current
        label_text = text if text is not None else f"当前使用节点：{current or '未选择'}"
        self.current_node_label.setText(label_text)
        self._refresh_log_box()

    def _configure_provider_menu(self, providers: list[str] | None = None) -> None:
        if self.provider_menu is None:
            return
        values = providers or ["全部订阅"] + sorted(
            {item["provider"] for item in self._all_candidates}
        )
        self.provider_menu.blockSignals(True)
        self.provider_menu.clear()
        self.provider_menu.addItems(values)
        if self.provider_filter not in values:
            self.provider_filter = "全部订阅"
        self.provider_menu.setCurrentText(self.provider_filter)
        self.provider_menu.blockSignals(False)

    def _update_candidate_summary(self) -> None:
        count = len(self._all_candidates)
        self.candidate_summary_label.setText(
            f"候选节点（{count} 个，仅从当前运行配置抽取）"
        )

    def refresh_groups(self) -> None:
        self._groups_request_id += 1
        request_id = self._groups_request_id
        self.refresh_button.setEnabled(False)

        def done(snapshot: tuple[dict, dict, str | None]) -> None:
            if request_id != self._groups_request_id:
                return
            groups, proxies, route_group = snapshot
            self._known_group_names = set(groups)
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
            self.group_menu.blockSignals(True)
            self.group_menu.clear()
            self.group_menu.addItems(names)
            self.group_menu.setCurrentText(selected)
            self.group_menu.blockSignals(False)
            current = str(groups.get(selected, {}).get("now") or "")
            self._set_current_node(current)
            self.refresh_button.setEnabled(True)
            self.refresh_candidates()

        def failed(exc: Exception) -> None:
            if request_id != self._groups_request_id:
                return
            self.refresh_button.setEnabled(True)
            QMessageBox.critical(self, "刷新策略组失败", str(exc))

        def load_groups() -> tuple[dict, dict, str | None]:
            client = self._client()
            proxies = client.proxies()
            groups = {name: item for name, item in proxies.items() if item.get("type") == "Selector"}
            route_group = resolve_route_group(client, self.config_data, groups, proxies)
            return groups, proxies, route_group

        self._background(load_groups, done, failed)

    def refresh_candidates(self) -> None:
        self._candidates_request_id += 1
        request_id = self._candidates_request_id
        self._capture_visible_selection()
        group = self.group_menu.currentText()
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
            if request_id != self._candidates_request_id or group != self.group_menu.currentText():
                return
            live_names = {item["name"] for item in items}
            if not self.selected_candidates.intersection(live_names):
                # 新分组或订阅整体替换后没有任何可用勾选时，默认全选全部节点。
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

        def failed(exc: Exception) -> None:
            if request_id != self._candidates_request_id or group != self.group_menu.currentText():
                return
            self._set_current_node("", "当前使用节点：读取失败")
            QMessageBox.critical(self, "刷新候选节点失败", str(exc))

        self._background(lambda: self._client().selector_group_snapshot(group), done, failed)

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
        for name, box in self.candidate_vars.items():
            if box.isChecked():
                self.selected_candidates.add(name)
            else:
                self.selected_candidates.discard(name)
        self._sync_group_selection_maps()

    def _toggle_candidate(self, name: str, checked: bool) -> None:
        shift = bool(QApplication.keyboardModifiers() & Qt.ShiftModifier)
        if shift and self._selection_anchor in self.candidate_vars:
            start = self._visible_candidate_names.index(self._selection_anchor)
            end = self._visible_candidate_names.index(name)
            for candidate_name in self._visible_candidate_names[min(start, end):max(start, end) + 1]:
                self.candidate_vars[candidate_name].setChecked(checked)
        else:
            self._selection_anchor = name
        self._capture_visible_selection()

    def _render_candidates(self) -> None:
        if self.nodes_frame is None or self.nodes_layout is None:
            return
        for child in self.nodes_frame.findChildren(QWidget):
            child.deleteLater()
        self.candidate_vars = {}
        self.node_status_labels = {}
        self.node_name_widgets = {}
        shown = [
            item for item in self._all_candidates
            if self.provider_filter == "全部订阅" or item["provider"] == self.provider_filter
        ]
        self._visible_candidate_names = [item["name"] for item in shown]
        if self._selection_anchor not in self._visible_candidate_names:
            self._selection_anchor = None
        for row, item in enumerate(shown):
            box = QCheckBox(item["name"])
            box.setChecked(item["name"] in self.selected_candidates)
            box.toggled.connect(lambda checked, name=item["name"]: self._toggle_candidate(name, checked))
            self.nodes_layout.addWidget(box, row, 0)
            self.candidate_vars[item["name"]] = box
            self.node_name_widgets[item["name"]] = box
            result = self.node_test_results.get(item["name"])
            status_text, status_color = self._result_display(result)
            if result:
                box.setStyleSheet(f"color: {status_color};")
            status = QLabel(status_text)
            status.setStyleSheet(f"color: {status_color};")
            status.setFixedWidth(72)
            status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.nodes_layout.addWidget(status, row, 1)
            self.node_status_labels[item["name"]] = status
            provider = QLabel(item["provider"])
            provider.setStyleSheet(f"color: {GRAY_TEXT};")
            self.nodes_layout.addWidget(provider, row, 2)

    @staticmethod
    def _result_display(result: dict[str, object] | None) -> tuple[str, str]:
        if result and result.get("status") == "testing":
            return "测试中", "#d97706"
        if result and result.get("status") == "ok":
            return f"{result['delay']} ms", "#16a34a"
        if result:
            return "失败", "#dc2626"
        return "", GRAY_TEXT

    def toggle_all(self) -> None:
        target = not all(box.isChecked() for box in self.candidate_vars.values())
        for box in self.candidate_vars.values():
            box.setChecked(target)
        self._selection_anchor = None
        self._capture_visible_selection()

    def _collect_config(self) -> MonitorConfig:
        try:
            interval = int(self.interval_entry.text())
            threshold = int(self.threshold_entry.text())
            if interval < 5 or threshold < 1:
                raise ValueError
        except ValueError:
            raise ValueError("间隔至少为 5 秒，失败阈值至少为 1")
        test_urls = [item.strip() for item in self.url_entry.text().split(",") if item.strip()]
        if not test_urls or any(not item.startswith(("http://", "https://")) for item in test_urls):
            raise ValueError("测试网址必须以 http:// 或 https:// 开头，多个网址用英文逗号分隔")
        self._capture_visible_selection()
        selected = [item["name"] for item in self._all_candidates if item["name"] in self.selected_candidates]
        config = copy.deepcopy(self.config_data)
        config.test_urls = list(dict.fromkeys(test_urls))
        config.interval_seconds = interval
        config.failure_threshold = threshold
        config.advanced_web_probe = self.advanced_probe_check.isChecked()
        config.group = self.group_menu.currentText()
        config.candidates = selected
        return config

    def save_config(self, notify: bool = True) -> MonitorConfig:
        self.config_data = self._collect_config()
        self.config_data.save()
        if notify:
            count = len(self.config_data.candidates)
            QMessageBox.information(
                self,
                "已保存",
                f"已保存 {count} 个候选节点"
                if count
                else "未勾选候选节点，将在策略组全部节点中探测轮换",
            )
        return self.config_data

    def _set_manual_buttons(self, enabled: bool) -> None:
        for button in (
            self.group_menu,
            self.refresh_button,
            self.test_current_button,
            self.switch_button,
            self.view_nodes_button,
            self.test_button,
        ):
            if button is not None:
                button.setEnabled(enabled)

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
        if self.test_button is not None:
            self.test_button.setText("立即测试")
        self.test_current_button.setText("测试当前节点")
        self.switch_button.setText("立即切换")

    def _next_manual_round(self) -> int:
        self._manual_round_id += 1
        return self._manual_round_id

    def _apply_node_result(self, name: str, result: dict[str, object]) -> None:
        self.node_test_results[name] = result
        status_text, status_color = self._result_display(result)
        label = self.node_status_labels.get(name)
        box = self.node_name_widgets.get(name)
        if label:
            label.setText(status_text)
            label.setStyleSheet(f"color: {status_color};")
        if box:
            box.setStyleSheet(f"color: {status_color};")

    def _append_manual_log(self, node: str, message: str) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self._manual_log_entries.append((node, f"{timestamp} [INFO] {message}"))
        self._manual_log_entries = self._manual_log_entries[-100:]
        self._refresh_log_box()

    def _refresh_log_box(self) -> None:
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
        self.log_box.setPlainText(text)

    def test_once(self) -> None:
        if self._manual_task_running:
            return
        try:
            config = self._collect_config()
        except Exception as exc:
            QMessageBox.critical(self, "配置错误", str(exc))
            return
        candidates = list(self._all_candidates)
        if not candidates:
            QMessageBox.warning(self, "没有节点", "当前策略组没有可测试的真实节点")
            return
        self._manual_task_running = True
        self._set_manual_buttons(False)
        self.test_button.setText(f"测试中 0/{len(candidates)}")
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
                if self.test_button is not None:
                    self.test_button.setText(f"测试中 {current_count}/{len(candidates)}")
            self._post(update)

        def worker():
            try:
                results = test_group_candidates(self._client(), config, candidates, report)
                success_count = sum(item["status"] == "ok" for item in results.values())
                self._post(lambda: QMessageBox.information(
                    self, "测试完成", f"成功 {success_count} 个，失败 {len(results) - success_count} 个"
                ))
            except Exception as exc:
                error_text = str(exc)
                self._post(lambda: QMessageBox.critical(self, "测试失败", error_text))
            finally:
                self._post(self._finish_manual_task)
        threading.Thread(target=worker, daemon=True).start()

    def test_current_node(self) -> None:
        if self._manual_task_running:
            return
        try:
            config = self._collect_config()
        except Exception as exc:
            QMessageBox.critical(self, "配置错误", str(exc))
            return

        self._manual_task_running = True
        self._set_manual_buttons(False)
        self.test_current_button.setText("测试中…")
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
                    if runtime_group == self.group_menu.currentText():
                        self._set_current_node(current)
                    self._apply_node_result(current, result)
                    if result["status"] == "ok":
                        self._append_manual_log(
                            current,
                            f"手动测试节点可用：{current}，最大延迟 {result['delay']} ms，明细={result['delays']}",
                        )
                        QMessageBox.information(self, "测试完成", f"当前节点可用，最大延迟 {result['delay']} ms")
                    else:
                        error = result.get("error", "未知错误")
                        self._append_manual_log(current, f"手动测试节点不可用：{current}（{error}）")
                        QMessageBox.warning(self, "测试完成", f"当前节点不可用：{error}")

                self._post(done)
            except Exception as exc:
                error_text = str(exc)
                self._post(lambda: QMessageBox.critical(self, "测试失败", error_text))
            finally:
                self._post(self._finish_manual_task)

        threading.Thread(target=worker, daemon=True).start()

    def switch_now(self) -> None:
        if self._manual_task_running:
            return
        try:
            config = self._collect_config()
        except Exception as exc:
            QMessageBox.critical(self, "配置错误", str(exc))
            return
        available_names = [item["name"] for item in self._all_candidates]
        candidate_names = [
            name for name in config.candidates if name in set(available_names)
        ] or available_names
        if not candidate_names:
            QMessageBox.warning(self, "没有候选节点", "当前策略组没有可测试的真实节点")
            return

        self._manual_task_running = True
        self._set_manual_buttons(False)
        self.switch_button.setText("切换中…")
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
                    self._post(lambda: self._handle_manual_switch(config.group, current, cached))
                    return

                config.candidates = candidate_names
                round_id = self._next_manual_round()

                def report(name, result):
                    nonlocal selected_result
                    result = dict(result)
                    cache_node_result(self.node_test_cache, name, result, round_id)
                    self._post(lambda node=name, item=result: self._apply_node_result(node, item))
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
                            self._post(lambda item=switched: self._handle_manual_switch(
                                config.group, current, item
                            ))

                results = probe_nodes_delays(client, config, candidate_names, report)
                if selected_result is None:
                    self._post(lambda: QMessageBox.warning(
                        self,
                        "切换失败",
                        "当前没有可用的候选节点，保持当前节点不变",
                    ))
            except Exception as exc:
                error_text = str(exc)
                self._post(lambda: QMessageBox.critical(self, "切换失败", error_text))
            finally:
                self._post(self._finish_manual_task)

        threading.Thread(target=worker, daemon=True).start()

    def _handle_manual_switch(self, group: str, previous: str, result: dict[str, object]) -> None:
        selected = str(result["selected"])
        if self.group_menu.currentText() == group:
            self._set_current_node(selected)
        self._apply_node_result(selected, {"status": "ok", "delay": result["delay"]})
        self._append_manual_log(
            selected,
            f"手动切换节点：{previous} -> {selected}（{result['delay']} ms，{result['cache_source']}缓存）",
        )
        self.switch_button.setText("已切换")

    def _is_running(self) -> bool:
        return self.service_manager.is_running()

    def toggle_service(self) -> None:
        if self._is_running():
            self.stop_service()
            return
        try:
            self.save_config(notify=False)
        except Exception as exc:
            QMessageBox.critical(self, "配置错误", str(exc))
            return
        try:
            self.process = self.service_manager.start()
        except ServiceManagerError as exc:
            QMessageBox.critical(self, "启动服务失败", str(exc))
            return
        self._schedule_status_refresh(500)

    def stop_service(self) -> None:
        # 监控进程要等本轮探测收尾才退出，同步停止会冻住界面。
        self.service_button.setEnabled(False)

        def worker() -> None:
            try:
                self.service_manager.stop()
            except ServiceManagerError as exc:
                self._post(lambda: self._finish_stop(exc))
            else:
                self._post(lambda: self._finish_stop(None))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_stop(self, exc: Exception | None) -> None:
        self.service_button.setEnabled(True)
        if exc is not None:
            QMessageBox.critical(self, "停止服务失败", str(exc))
            return
        self.process = None
        self.refresh_status()

    def _refresh_current_node(self) -> None:
        group = self.group_menu.currentText()
        if self._current_node_refreshing or group in {"", "正在读取…", "没有 Selector 策略组"}:
            return
        self._current_node_refreshing = True

        def done(snapshot: tuple[set[str], str | None, set[str] | None]) -> None:
            self._current_node_refreshing = False
            if self._closing or self._manual_task_running:
                return
            group_names, current, live_names = snapshot
            if group_names != self._known_group_names or current is None:
                # 订阅切换导致策略组增删或当前组消失时，重新解析待切换组。
                self.refresh_groups()
                return
            if (
                live_names is not None
                and group == self._candidate_group
                and live_names != self._live_candidate_names
            ):
                # 组名不变但订阅节点整体替换时，自动刷新候选列表。
                self.refresh_candidates()
                return
            if group == self.group_menu.currentText():
                self._set_current_node(current)

        def failed(_exc: Exception) -> None:
            self._current_node_refreshing = False
            if group == self.group_menu.currentText():
                self._set_current_node("", "当前使用节点：无法读取")

        def read_current() -> tuple[set[str], str | None, set[str] | None]:
            client = self._client()
            proxies = client.proxies()
            group_names = {
                name for name, item in proxies.items() if item.get("type") == "Selector"
            }
            if group not in proxies:
                return group_names, None, None
            current, live = client.selector_group_snapshot(group, proxies=proxies)
            return group_names, current, {item["name"] for item in live}

        self._background(read_current, done, failed)

    def refresh_status(self) -> None:
        running = self._is_running()
        if running:
            self.service_button.setText("停止服务")
            self.service_button.setStyleSheet("background-color: #dc2626; color: white;")
        else:
            self.service_button.setText("启动服务")
            self.service_button.setStyleSheet("background-color: #16a34a; color: white;")
        self.status_label.setText("服务运行中" if running else "服务未运行")
        self._refresh_current_node()
        self._refresh_log_box()
        self._schedule_status_refresh()

    def _schedule_status_refresh(self, delay_ms: int = 3000) -> None:
        """确保状态刷新始终只有一个待执行的定时回调。"""
        self._status_timer.stop()
        self._status_timer.start(delay_ms)

    def _run_status_refresh(self) -> None:
        self.refresh_status()

    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self._status_timer.stop()
        event.accept()
        # 探测线程池含非守护线程，解释器退出时会等待全部探测任务完成；
        # 直接结束进程避免关闭窗口后假死。
        os._exit(0)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Clash Verge 节点守护")
    app.setStyle("Fusion")
    _apply_palette(app)
    window = ConfigApp()
    window.show()
    app.exec()


if __name__ == "__main__":
    main()