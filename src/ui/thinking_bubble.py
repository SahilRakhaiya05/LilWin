
import random
from typing import List, Optional

from PyQt6.QtCore import QEasingCurve, QPoint, QRect, QRectF, QPropertyAnimation, Qt, QTimer
from PyQt6.QtGui import QColor, QGuiApplication, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

_DEFAULT_ACCENT = "#FF8C69"

class ThinkingBubble(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.layout = QVBoxLayout(self)
        self._bubble_radius = 16
        self._arrow_size = 10
        self._arrow_edge = "bottom"
        self.layout.setContentsMargins(16, 14, 16, 22)
        self.label = QLabel(self)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True)
        self.label.setMinimumWidth(84)
        self.label.setMaximumWidth(220)
        self.layout.addWidget(self.label)
        self._anchor_rect = QRect()
        self._taskbar_edge: Optional[int] = None
        self._completion = False
        self._accent = _DEFAULT_ACCENT
        self.theme = "Peach"
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(160)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

        self.phrases: List[str] = [
            "Thinking...", "Pondering...", "Analyzing...", "Consulting the oracle...",
            "Crunching numbers...", "Decoding signals...", "Brewing ideas...",
            "Searching the void...", "Gathering thoughts...", "Connecting dots..."
        ]
        self.completion_phrases: List[str] = [
            "Done!", "All set!", "Ready!", "Finished!", "Ta-da!", "Boom!"
        ]

        # Timers must exist before any show_for_rect / hide_bubble call, so
        # they're created here rather than in set_personality (which might be
        # skipped entirely if the character has no personality block).
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._advance)
        self.completion_timer = QTimer(self)
        self.completion_timer.setSingleShot(True)
        self.completion_timer.timeout.connect(self.hide_bubble)

        self._apply_palette()
        # Start hidden — nothing to say until the session actually becomes busy.
        self.hide()

    def _theme_tokens(self, theme: str) -> dict:
        presets = {
            "Peach": {
                "bubble_bg": "#FFF8EF",
                "text": "#332830",
                "completion_bg": "#F2FFF3",
                "completion_text": "#1E3B22",
                "completion_border": "#2E9E4B",
            },
            "Midnight": {
                "bubble_bg": "#1C1D26",
                "text": "#ECECF4",
                "completion_bg": "#17221A",
                "completion_text": "#D8F3DD",
                "completion_border": "#66BB6A",
            },
            "Cloud": {
                "bubble_bg": "#FAFBFE",
                "text": "#26303B",
                "completion_bg": "#EFF8F0",
                "completion_text": "#245B2D",
                "completion_border": "#2E9E4B",
            },
            "Moss": {
                "bubble_bg": "#F7FBF4",
                "text": "#223028",
                "completion_bg": "#EEF8EB",
                "completion_text": "#244F2B",
                "completion_border": "#43A047",
            },
        }
        return presets.get(theme, presets["Peach"])

    def set_theme(self, theme: str) -> None:
        self.theme = str(theme or "Peach")
        self._apply_palette()
        self.update()

    def set_personality(
        self,
        thinking_phrases: List[str],
        done_phrases: List[str],
        accent_color: Optional[str] = None,
    ) -> None:
        if thinking_phrases:
            self.phrases = list(thinking_phrases)
        if done_phrases:
            self.completion_phrases = list(done_phrases)
        if accent_color:
            qc = QColor(accent_color)
            if qc.isValid():
                self._accent = accent_color
        self._apply_palette()

    def _apply_palette(self):
        t = self._theme_tokens(self.theme)
        border = t["completion_border"] if self._completion else self._accent
        text = t["completion_text"] if self._completion else t["text"]
        bg = t["completion_bg"] if self._completion else t["bubble_bg"]
        self.setStyleSheet(
            f"""
            QLabel {{
                background: transparent;
                color: {text};
                border: none;
                padding: 0px;
                font-size: 12px;
                font-weight: 700;
                line-height: 1.2;
            }}
            """
        )
        self._bubble_bg = QColor(bg)
        self._bubble_border = QColor(border)

    def _update_layout_margins(self) -> None:
        top = 14 + (self._arrow_size if self._arrow_edge == "top" else 0)
        bottom = 14 + (self._arrow_size if self._arrow_edge == "bottom" else 0)
        self.layout.setContentsMargins(16, top, 16, bottom)

    def _recompute_size(self) -> None:
        self._update_layout_margins()
        self.label.adjustSize()
        hint = self.layout.sizeHint()
        width = max(100, min(250, hint.width()))
        height = max(44, hint.height())
        self.resize(width, height)

    def _place_near_anchor(self, anchor_rect: QRect, taskbar_edge: Optional[int]) -> QPoint:
        self._taskbar_edge = taskbar_edge
        # Prefer the bubble above the character with a bottom arrow. Only
        # flip below when there isn't enough room on screen.
        self._arrow_edge = "bottom"
        self._recompute_size()
        w = max(self.width(), 1)
        h = max(self.height(), 1)

        center = anchor_rect.center()
        screen = QGuiApplication.screenAt(center) or QGuiApplication.primaryScreen()
        available = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        margin = 10
        gap = 10

        x = center.x() - (w // 2)
        x = max(available.left() + margin, min(x, available.right() - w - margin))

        above_y = anchor_rect.top() - h - gap
        below_y = anchor_rect.bottom() + gap
        if above_y >= available.top() + margin:
            y = above_y
            self._arrow_edge = "bottom"
        else:
            self._arrow_edge = "top"
            self._recompute_size()
            h = max(self.height(), 1)
            below_y = anchor_rect.bottom() + gap
            y = min(below_y, available.bottom() - h - margin)

        return QPoint(x, y)

    def show_above(self, pos: QPoint):
        """Place the bubble above ``pos`` (global / desktop coordinates, e.g. top-center of the agent)."""
        anchor = QRect(pos.x() - 1, pos.y() - 1, 2, 2)
        self.show_for_rect(anchor, None)

    def show_for_rect(self, anchor_rect: QRect, taskbar_edge: Optional[int]):
        self._anchor_rect = QRect(anchor_rect)
        self._taskbar_edge = taskbar_edge
        self.completion_timer.stop()
        self.update_phrase()
        self.move(self._place_near_anchor(anchor_rect, taskbar_edge))
        self._fade.stop()
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        self.timer.start(2000)

    def show_completion_for_rect(self, anchor_rect: QRect, taskbar_edge: Optional[int], text: Optional[str] = None):
        self._anchor_rect = QRect(anchor_rect)
        self._taskbar_edge = taskbar_edge
        self._completion = True
        self._apply_palette()
        self.label.setText(text or random.choice(self.completion_phrases))
        self._recompute_size()
        self.move(self._place_near_anchor(anchor_rect, taskbar_edge))
        self._fade.stop()
        self.setWindowOpacity(0.0)
        self.show()
        self.raise_()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()
        self.timer.stop()
        self.completion_timer.start(3000)

    def _advance(self):
        self.update_phrase()

    def update_phrase(self):
        if self._completion:
            return
        self._completion = False
        self._apply_palette()
        self.label.setText(random.choice(self.phrases))
        self._recompute_size()
        if self.isVisible() and not self._anchor_rect.isNull():
            self.move(self._place_near_anchor(self._anchor_rect, self._taskbar_edge))

    def move_for_rect(self, anchor_rect: QRect, taskbar_edge: Optional[int]):
        self._anchor_rect = QRect(anchor_rect)
        self._taskbar_edge = taskbar_edge
        self.move(self._place_near_anchor(anchor_rect, taskbar_edge))

    def hide_bubble(self):
        self.timer.stop()
        self.completion_timer.stop()
        self._completion = False
        self._apply_palette()
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(self._bubble_bg)
        painter.setPen(QPen(self._bubble_border, 1.4))

        top_inset = self._arrow_size if self._arrow_edge == "top" else 0
        bottom_inset = self._arrow_size if self._arrow_edge == "bottom" else 0
        rect = QRectF(self.rect().adjusted(1, 1 + top_inset, -1, -1 - bottom_inset))
        path = QPainterPath()
        path.addRoundedRect(rect, self._bubble_radius, self._bubble_radius)

        tail = QPainterPath()
        cx = rect.center().x()
        tail_half = 8
        if self._arrow_edge == "top":
            tail.moveTo(cx - tail_half, rect.top())
            tail.lineTo(cx, rect.top() - self._arrow_size)
            tail.lineTo(cx + tail_half, rect.top())
        else:
            tail.moveTo(cx - tail_half, rect.bottom())
            tail.lineTo(cx, rect.bottom() + self._arrow_size)
            tail.lineTo(cx + tail_half, rect.bottom())
        path.addPath(tail)
        painter.drawPath(path)
        super().paintEvent(event)
