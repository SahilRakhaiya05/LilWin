"""Settings editor: gateway URL, auth token (secrets store), sizes, timings,
and a provider→character mapping table so each AI provider gets its signature
walker."""

from __future__ import annotations

import logging
import os
import sys
import subprocess
from typing import Any, Dict

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from services.character_registry import get_registry
from utils.config import ConfigManager
from utils.secrets import SecretsManager

logger = logging.getLogger(__name__)


def _is_valid_ws_url(url: str) -> bool:
    return url.startswith("ws://") or url.startswith("wss://")


class SettingsDialog(QDialog):
    @staticmethod
    def _load_character_visibility_mode(data: Dict[str, Any]) -> str:
        v = str(data.get("characterVisibilityMode", "")).lower()
        if v in ("always", "external_cli", "standalone_external_cli"):
            return v
        if data.get("hideCharacterUntilExternalCli"):
            return "external_cli"
        return "always"

    def __init__(self, config: ConfigManager, parent=None):
        super().__init__(parent)
        self.config = config
        self._secrets = SecretsManager()
        self._registry = get_registry()
        self.setWindowTitle("LilWin settings")
        self.setMinimumWidth(520)

        data = config.load()
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Keep the everyday controls on the first tab. Advanced terminal-link, "
            "OpenClaw, and motion settings are tucked away so this window stays simple."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        tabs = QTabWidget(self)
        tabs.addTab(self._build_essentials_tab(data), "Essentials")
        tabs.addTab(self._build_advanced_tab(data), "Advanced")
        tabs.addTab(self._build_characters_tab(data), "Characters")
        layout.addWidget(tabs, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_essentials_tab(self, data: Dict[str, Any]) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)

        self.character_size = QComboBox()
        self.character_size.addItems(["small", "medium", "large"])
        size_value = str(data.get("characterSize", "medium")).lower()
        idx = max(0, self.character_size.findText(size_value))
        self.character_size.setCurrentIndex(idx)
        form.addRow("Character size:", self.character_size)

        self.character_visibility = QComboBox()
        self.character_visibility.addItem("Always show desktop character", "always")
        self.character_visibility.addItem("Only while provider runs in any terminal", "external_cli")
        self.character_visibility.addItem("Only in standalone terminals (skip IDE hosts)", "standalone_external_cli")
        vis_mode = self._load_character_visibility_mode(data)
        idx = max(0, self.character_visibility.findData(vis_mode))
        self.character_visibility.setCurrentIndex(idx)
        form.addRow("Desktop character:", self.character_visibility)

        self.monitor_external = QCheckBox("Show activity from external CLI sessions")
        self.monitor_external.setChecked(bool(data.get("monitorExternalCli", True)))
        form.addRow("External monitor:", self.monitor_external)

        self.show_idle_roster = QCheckBox("Auto-deploy every character on startup")
        self.show_idle_roster.setChecked(bool(data.get("showIdleRosterCharacters", False)))
        form.addRow("Extra walkers:", self.show_idle_roster)

        hint = QLabel(
            "Recommended: leave auto-deploy off. Use Tray → Walkers → a character "
            "→ Show on desktop / a provider to add walkers only when you need them."
        )
        hint.setWordWrap(True)
        form.addRow("", hint)
        return page

    def _build_advanced_tab(self, data: Dict[str, Any]) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)

        provider_group = QGroupBox("OpenClaw gateway")
        provider_form = QFormLayout(provider_group)
        self.gateway = QLineEdit(data.get("gatewayURL", "ws://localhost:3001"))
        self.token = QLineEdit(self._secrets.auth_token())
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("Optional auth token")
        provider_form.addRow("Gateway URL:", self.gateway)
        provider_form.addRow("Auth token:", self.token)
        gateway_hint = QLabel("Only OpenClaw uses these fields. Local CLIs come from PATH.")
        gateway_hint.setWordWrap(True)
        provider_form.addRow("", gateway_hint)
        layout.addWidget(provider_group)

        link_group = QGroupBox("Terminal linking")
        link_form = QFormLayout(link_group)
        self.show_cursor_terminal_link = QCheckBox("Link Cursor / VS Code / other integrated terminals")
        self.show_cursor_terminal_link.setChecked(bool(data.get("showCursorTerminalLink", False)))
        link_form.addRow("Link mode:", self.show_cursor_terminal_link)
        self.link_bridge_auto_paste = QCheckBox("Auto-paste into the linked terminal after Enter")
        self.link_bridge_auto_paste.setChecked(bool(data.get("linkBridgeAutoPaste", True)))
        link_form.addRow("Paste bridge:", self.link_bridge_auto_paste)
        self.link_bridge_send_enter = QCheckBox("Also press Enter after paste")
        self.link_bridge_send_enter.setChecked(bool(data.get("linkBridgeSendEnter", True)))
        link_form.addRow("", self.link_bridge_send_enter)
        link_hint = QLabel(
            "When linking is off, the provider runs inside the app. In linked mode, "
            "single /commands go to the real CLI and //commands stay local."
        )
        link_hint.setWordWrap(True)
        link_form.addRow("", link_hint)
        self.show_cursor_terminal_link.toggled.connect(self._sync_link_controls)
        self.link_bridge_auto_paste.toggled.connect(self._sync_link_controls)
        self._sync_link_controls()
        layout.addWidget(link_group)

        motion_group = QGroupBox("Motion")
        motion_form = QFormLayout(motion_group)
        self.walk_duration = QDoubleSpinBox()
        self.walk_duration.setRange(4.0, 30.0)
        self.walk_duration.setSingleStep(0.5)
        self.walk_duration.setSuffix(" s")
        self.walk_duration.setValue(float(data.get("walkDurationSec", 10.0)))
        motion_form.addRow("Walk duration:", self.walk_duration)

        self.pause_min = QDoubleSpinBox()
        self.pause_min.setRange(0.2, 60.0)
        self.pause_min.setSingleStep(0.5)
        self.pause_min.setSuffix(" s")
        self.pause_min.setValue(float(data.get("pauseMinSec", 5.0)))
        motion_form.addRow("Pause min:", self.pause_min)

        self.pause_max = QDoubleSpinBox()
        self.pause_max.setRange(0.2, 60.0)
        self.pause_max.setSingleStep(0.5)
        self.pause_max.setSuffix(" s")
        self.pause_max.setValue(float(data.get("pauseMaxSec", 12.0)))
        motion_form.addRow("Pause max:", self.pause_max)

        self.short_pause_min = QDoubleSpinBox()
        self.short_pause_min.setRange(0.2, 30.0)
        self.short_pause_min.setSingleStep(0.5)
        self.short_pause_min.setSuffix(" s")
        self.short_pause_min.setValue(float(data.get("shortPauseMinSec", 2.0)))
        motion_form.addRow("Chat-close pause min:", self.short_pause_min)

        self.short_pause_max = QDoubleSpinBox()
        self.short_pause_max.setRange(0.2, 30.0)
        self.short_pause_max.setSingleStep(0.5)
        self.short_pause_max.setSuffix(" s")
        self.short_pause_max.setValue(float(data.get("shortPauseMaxSec", 5.0)))
        motion_form.addRow("Chat-close pause max:", self.short_pause_max)
        layout.addWidget(motion_group)
        layout.addStretch(1)
        return page

    def _build_characters_tab(self, data: Dict[str, Any]) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.addWidget(self._build_mapping_group(data))

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel(f"Config file: {self.config.config_path}"))
        open_btn = QPushButton("Open config folder")
        open_btn.clicked.connect(self._open_config_folder)
        path_row.addWidget(open_btn)
        skills_btn = QPushButton("Open skills folder")
        skills_btn.clicked.connect(self._open_skills_folder)
        path_row.addWidget(skills_btn)
        layout.addLayout(path_row)

        skills_hint = QLabel(
            "Character skills live in skills/<character>/SKILL.md and describe how that "
            "walker should be deployed: provider, cwd, and session style."
        )
        skills_hint.setWordWrap(True)
        layout.addWidget(skills_hint)
        layout.addStretch(1)
        return page

    def _sync_link_controls(self) -> None:
        linking_on = bool(self.show_cursor_terminal_link.isChecked())
        self.link_bridge_auto_paste.setEnabled(linking_on)
        self.link_bridge_send_enter.setEnabled(linking_on and self.link_bridge_auto_paste.isChecked())

    def _build_mapping_group(self, data: Dict[str, Any]) -> QGroupBox:
        group = QGroupBox("Provider → character mapping")
        mapping = dict(data.get("providerCharacterMap") or {})
        grid = QFormLayout(group)
        self._mapping_selectors: Dict[str, QComboBox] = {}
        providers = list(data.get("providers") or ["Claude", "Codex", "Copilot", "Gemini", "OpenCode", "OpenClaw"])
        characters = self._registry.list()
        labels = [(c.name, c.display_name) for c in characters]
        labels.insert(0, ("", "— keep current —"))
        for provider in providers:
            combo = QComboBox()
            for char_name, label in labels:
                combo.addItem(label, char_name)
            selected_name = str(mapping.get(provider) or "").strip().lower()
            idx = max(0, combo.findData(selected_name))
            combo.setCurrentIndex(idx)
            grid.addRow(provider, combo)
            self._mapping_selectors[provider] = combo
        return group

    def _open_config_folder(self) -> None:
        folder = os.path.dirname(self.config.config_path)
        os.makedirs(folder, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", folder])

    def _open_skills_folder(self) -> None:
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        folder = os.path.join(base_dir, "skills")
        os.makedirs(folder, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(folder)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", folder])

    def _save(self) -> None:
        gateway = self.gateway.text().strip() or "ws://localhost:3001"
        if not _is_valid_ws_url(gateway):
            QMessageBox.warning(
                self,
                "Invalid gateway URL",
                "Gateway URL must start with ws:// or wss://",
            )
            return

        data = self.config.load()
        data["gatewayURL"] = gateway
        data["characterSize"] = self.character_size.currentText()
        data["characterVisibilityMode"] = str(self.character_visibility.currentData())
        data["monitorExternalCli"] = bool(self.monitor_external.isChecked())
        data["showCursorTerminalLink"] = bool(self.show_cursor_terminal_link.isChecked())
        data["showIdleRosterCharacters"] = bool(self.show_idle_roster.isChecked())
        data["linkBridgeAutoPaste"] = bool(self.link_bridge_auto_paste.isChecked())
        data["linkBridgeSendEnter"] = bool(self.link_bridge_send_enter.isChecked())
        data["walkDurationSec"] = float(self.walk_duration.value())
        data["pauseMinSec"] = float(self.pause_min.value())
        data["pauseMaxSec"] = float(max(self.pause_min.value(), self.pause_max.value()))
        data["shortPauseMinSec"] = float(self.short_pause_min.value())
        data["shortPauseMaxSec"] = float(max(self.short_pause_min.value(), self.short_pause_max.value()))
        mapping = dict(data.get("providerCharacterMap") or {})
        for provider, combo in self._mapping_selectors.items():
            value = str(combo.currentData() or "").strip().lower()
            if value:
                mapping[provider] = value
            else:
                mapping.pop(provider, None)
        data["providerCharacterMap"] = mapping
        self.config.save(data)

        self._secrets.set_auth_token(self.token.text().strip())
        logger.info("Settings saved; mapping=%s", mapping)
        self.accept()
