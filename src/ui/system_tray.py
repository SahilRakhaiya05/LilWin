
import os
from PyQt6.QtWidgets import QSystemTrayIcon, QMenu
from PyQt6.QtGui import QIcon, QAction, QActionGroup, QPixmap, QPainter, QColor, QPen, QBrush
from PyQt6.QtCore import Qt, pyqtSignal, QRectF

from __version__ import VERSION
from services.character_registry import get_registry

class LilAgentsTray(QSystemTrayIcon):
    provider_changed = pyqtSignal(str)
    character_changed = pyqtSignal(str)
    character_toggled = pyqtSignal(str, bool)
    theme_changed = pyqtSignal(str)
    open_chat_requested = pyqtSignal()
    spawn_walker_requested = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent = parent

        # Icon setup (fallback if assets are not converted yet)
        assets_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
        icon_path = os.path.join(assets_dir, "menuicon.png")
        if os.path.isfile(icon_path):
            self.setIcon(QIcon(icon_path))
        else:
            self.setIcon(self._default_tray_icon())
        self.setToolTip(f"LilWin v{VERSION}")
        
        # Menu setup
        self.menu = QMenu()
        
        # Provider submenu — this now sets the default provider for newly
        # spawned walkers. Per-walker switching happens from that walker's
        # own popover (`/provider <name>`).
        self.provider_menu = self.menu.addMenu("Default Provider")
        self.providers = ["Claude", "Codex", "Copilot", "Gemini", "OpenCode", "OpenClaw"]
        self.provider_group = QActionGroup(self)
        self.provider_group.setExclusive(True)
        for provider in self.providers:
            action = QAction(provider, self)
            action.setCheckable(True)
            if provider == "Claude":
                action.setChecked(True)
            action.triggered.connect(lambda checked, p=provider: self.provider_changed.emit(p) if checked else None)
            self.provider_group.addAction(action)
            self.provider_menu.addAction(action)
            
        # Walkers: one menu — per character: show/hide + add session (provider).
        self.walkers_menu = self.menu.addMenu("Walkers")
        registry = get_registry()
        registry_chars = registry.list()
        self.character_group = QActionGroup(self)
        self.character_group.setExclusive(False)
        self.character_actions: dict[str, QAction] = {}
        if not registry_chars:
            noop = QAction("(no characters configured)", self)
            noop.setEnabled(False)
            self.walkers_menu.addAction(noop)
        else:
            for i, c in enumerate(registry_chars):
                char_menu = self.walkers_menu.addMenu(c.display_name)
                vis = QAction("Show on desktop", self)
                vis.setCheckable(True)
                if i == 0:
                    vis.setChecked(True)
                vis.toggled.connect(
                    lambda checked, key=c.name: self.character_toggled.emit(
                        key, bool(checked)
                    )
                )
                self.character_group.addAction(vis)
                char_menu.addAction(vis)
                char_menu.addSeparator()
                for provider in self.providers:
                    act = QAction(provider, self)
                    act.triggered.connect(
                        lambda _checked=False, name=c.name, p=provider: self.spawn_walker_requested.emit(
                            name, p
                        )
                    )
                    char_menu.addAction(act)
                self.character_actions[c.name.lower()] = vis

        self.theme_menu = self.menu.addMenu("Theme")
        self.themes = ["Peach", "Midnight", "Cloud", "Moss"]
        self.theme_group = QActionGroup(self)
        self.theme_group.setExclusive(True)
        for theme in self.themes:
            action = QAction(theme, self)
            action.setCheckable(True)
            if theme == "Peach":
                action.setChecked(True)
            action.triggered.connect(lambda checked, t=theme: self.theme_changed.emit(t) if checked else None)
            self.theme_group.addAction(action)
            self.theme_menu.addAction(action)
            
        self.menu.addSeparator()

        self.open_chat_action = QAction("Open chat", self)
        self.open_chat_action.triggered.connect(self.open_chat_requested.emit)
        self.menu.insertAction(self.provider_menu.menuAction(), self.open_chat_action)
        self.menu.insertSeparator(self.provider_menu.menuAction())

        # Settings action
        self.settings_action = QAction("Settings", self)
        self.menu.addAction(self.settings_action)
        
        # Exit action
        self.exit_action = QAction("Exit", self)
        self.exit_action.triggered.connect(self.parent.quit)
        self.menu.addAction(self.exit_action)
        
        self.setContextMenu(self.menu)
        self.activated.connect(self._on_tray_activated)
        self.show()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.open_chat_requested.emit()

    @staticmethod
    def _default_tray_icon() -> QIcon:
        size = 64
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(60, 60, 60), 2))
        p.setBrush(QBrush(QColor(255, 140, 105)))
        p.drawEllipse(6, 6, size - 12, size - 12)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(40, 40, 40)))
        p.drawEllipse(20, 22, 8, 9)
        p.drawEllipse(36, 22, 8, 9)
        p.setPen(QPen(QColor(40, 40, 40), 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(22, 30, 20, 14), 200 * 16, 140 * 16)
        p.end()
        return QIcon(pm)
