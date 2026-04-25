import json
import math
import os
import re

import markdown
from html import escape
from typing import Optional, List, Tuple

try:  # Syntax highlighting for fenced code blocks.
    from pygments import highlight as _pyg_highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import get_lexer_by_name, guess_lexer
    from pygments.util import ClassNotFound as _PygClassNotFound

    _PYG_FORMATTER = HtmlFormatter(nowrap=True, noclasses=True, style="monokai")
    _PYG_AVAILABLE = True
    _PYG_LEXER_CACHE: dict = {}  # lang (lowercase) -> Lexer | None (miss)
except Exception:  # pragma: no cover - optional dependency
    _PYG_AVAILABLE = False
    _PYG_LEXER_CACHE = {}


def _cached_lexer_by_name(lang: str):
    """Return a Pygments lexer for *lang*, caching both hits and misses.

    Pygments' ``get_lexer_by_name`` is not free: every call imports plugin
    entrypoints and normalises aliases. Each rendered code block used to
    call it afresh, which showed up as a perceptible hitch when the
    assistant streamed a reply with many small fenced blocks. The cache
    key is the lowercased language tag; ``None`` entries mean "no such
    lexer — don't keep retrying".
    """
    if not _PYG_AVAILABLE:
        return None
    key = (lang or "").strip().lower()
    if key in _PYG_LEXER_CACHE:
        return _PYG_LEXER_CACHE[key]
    try:
        lex = get_lexer_by_name(key, stripall=False) if key else None
    except _PygClassNotFound:
        lex = None
    except Exception:
        lex = None
    _PYG_LEXER_CACHE[key] = lex
    return lex

from utils.window_placement import point_for_window_near_anchor

from PyQt6.QtCore import QPoint, QRect, QRectF, QStringListModel, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTransform,
)
from PyQt6.QtWidgets import (
    QCompleter,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """Convert `#RRGGBB` (or `#RGB`) to an (r, g, b) triple.

    Qt stylesheets can't interpolate hex colors into `rgba()`, so anywhere
    we want an accent tint with alpha we pre-compute the RGB components.
    """
    h = (hex_str or "").lstrip("#").strip()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return (128, 128, 128)
    try:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    except ValueError:
        return (128, 128, 128)


# ---------------------------------------------------------------------------
# Per-provider visual identity — each CLI gets its own accent + border width,
# so Claude, Gemini, Codex, Copilot, OpenCode, OpenClaw all feel distinct at a
# glance. Values are deliberate: thinner borders read calmer (Claude/Copilot),
# thicker reads bolder (Codex/OpenClaw).
# ---------------------------------------------------------------------------
_PROVIDER_VISUAL = {
    "Claude":   {"accent": "#D97757", "border_width": 2.0, "dot": "●"},
    "Gemini":   {"accent": "#4285F4", "border_width": 2.5, "dot": "◆"},
    "Codex":    {"accent": "#10A37F", "border_width": 3.0, "dot": "▲"},
    "Copilot":  {"accent": "#1F6FEB", "border_width": 2.0, "dot": "◉"},
    "OpenCode": {"accent": "#8A5CF6", "border_width": 2.5, "dot": "◈"},
    "OpenClaw": {"accent": "#FF5D2E", "border_width": 3.0, "dot": "◆"},
}
_DEFAULT_VISUAL = {"accent": "#D94D6A", "border_width": 2.0, "dot": "●"}

# PNG marks in ``assets/pos_logo`` (filename stem matches provider keys).
_POS_LOGO_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "assets", "pos_logo")
)
_PROVIDER_LOGO_FILES = {
    "Claude": "claude.png",
    "Gemini": "gemini.png",
    "Codex": "codex.png",
    "Copilot": "copilot.png",
    "OpenCode": "opencode.png",
    "OpenClaw": "openclaw.png",
}

# Compact mark next to 13px title — cap-height scale, small rounded chip (see UI ref).
_HEADER_LOGO_SLOT_W = 24
_HEADER_LOGO_SLOT_H = 18
_HEADER_LOGO_INSET_X = 2
_HEADER_LOGO_INSET_Y = 2

# Bump when sizing rules change so disk cache entries are not reused incorrectly.
_LOGO_PIPELINE_VERSION = 5

# Cached final slot pixmaps: ``(file path, mtime, pipeline version)`` -> QPixmap
_PROVIDER_LOGO_SLOT_CACHE: dict[tuple[str, float, int], QPixmap] = {}


def _alpha_bbox_argb32(img: QImage, alpha_threshold: int) -> Optional[QRect]:
    w, h = img.width(), img.height()
    min_x, min_y = w, h
    max_x, max_y = -1, -1
    for y in range(h):
        for x in range(w):
            if img.pixelColor(x, y).alpha() > alpha_threshold:
                if x < min_x:
                    min_x = x
                if y < min_y:
                    min_y = y
                if x > max_x:
                    max_x = x
                if y > max_y:
                    max_y = y
    if max_x < min_x:
        return None
    return QRect(min_x, min_y, max_x - min_x + 1, max_y - min_y + 1)


def _trim_transparent_margins(pm: QPixmap, alpha_threshold: int = 14) -> QPixmap:
    """Drop empty padding around raster logos so contain/cover uses real artwork bounds."""
    if pm.isNull() or pm.width() <= 0 or pm.height() <= 0:
        return pm
    w, h = pm.width(), pm.height()
    max_side = 280
    if max(w, h) > max_side:
        scale = max_side / max(w, h)
        probe = pm.scaled(
            max(1, int(round(w * scale))),
            max(1, int(round(h * scale))),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        img = probe.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        rect = _alpha_bbox_argb32(img, alpha_threshold)
        if rect is None:
            return pm
        inv_x = w / max(probe.width(), 1)
        inv_y = h / max(probe.height(), 1)
        min_x = max(0, int(math.floor(rect.x() * inv_x)))
        min_y = max(0, int(math.floor(rect.y() * inv_y)))
        max_x = min(w - 1, int(math.ceil((rect.x() + rect.width()) * inv_x)) - 1)
        max_y = min(h - 1, int(math.ceil((rect.y() + rect.height()) * inv_y)) - 1)
        if max_x < min_x or max_y < min_y:
            return pm
        rw = max_x - min_x + 1
        rh = max_y - min_y + 1
        full = pm.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        return QPixmap.fromImage(full.copy(QRect(min_x, min_y, rw, rh)))

    img = pm.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    rect = _alpha_bbox_argb32(img, alpha_threshold)
    if rect is None:
        return pm
    return QPixmap.fromImage(img.copy(rect))


def _header_logo_slot_pixmap(pm: QPixmap, slot_w: int, slot_h: int) -> QPixmap:
    """Scale *pm* to **fit inside** an inset frame, centered — padded like the UI reference.

    Uses uniform scale ``min(inner_w/w, inner_h/h)`` (letterbox / contain), not cover,
    so wide marks and tall marks share the same quiet footprint and clear margins.
    """
    if pm.isNull() or pm.width() <= 0 or pm.height() <= 0:
        return pm
    inset_x = _HEADER_LOGO_INSET_X
    inset_y = _HEADER_LOGO_INSET_Y
    inner_w = max(1, slot_w - 2 * inset_x)
    inner_h = max(1, slot_h - 2 * inset_y)
    w, h = pm.width(), pm.height()
    scale = min(inner_w / w, inner_h / h)
    scaled = pm.transformed(
        QTransform().scale(scale, scale),
        Qt.TransformationMode.SmoothTransformation,
    )
    sw, sh = scaled.width(), scaled.height()
    out = QPixmap(slot_w, slot_h)
    out.fill(Qt.GlobalColor.transparent)
    if sw < 1 or sh < 1:
        return out
    ox = (slot_w - sw) // 2
    oy = (slot_h - sh) // 2
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.drawPixmap(ox, oy, scaled)
    painter.end()
    return out


def _provider_logo_pixmap(provider: str) -> Optional[QPixmap]:
    fn = _PROVIDER_LOGO_FILES.get(provider)
    if not fn:
        return None
    path = os.path.join(_POS_LOGO_DIR, fn)
    if not os.path.isfile(path):
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    cache_key = (path, mtime, _LOGO_PIPELINE_VERSION)
    cached = _PROVIDER_LOGO_SLOT_CACHE.get(cache_key)
    if cached is not None and not cached.isNull():
        return cached

    pm = QPixmap(path)
    if pm.isNull():
        return None
    pm = _trim_transparent_margins(pm)
    slot = _header_logo_slot_pixmap(pm, _HEADER_LOGO_SLOT_W, _HEADER_LOGO_SLOT_H)
    if not slot.isNull():
        _PROVIDER_LOGO_SLOT_CACHE[cache_key] = slot
    return slot


def _visual_for(provider: str) -> dict:
    return _PROVIDER_VISUAL.get(provider, _DEFAULT_VISUAL)


def _current_ui_user_name() -> str:
    """Short display name for the local user shown above sent messages."""
    name = (os.environ.get("USER") or os.environ.get("USERNAME") or "").strip()
    if "\\" in name:
        name = name.rsplit("\\", 1)[-1]
    if name.islower():
        name = name.title()
    return name or "You"


def _escape_text_with_breaks(text: str) -> str:
    return escape(text).replace("\n", "<br>")


# Logged in ``_message_log`` as sender; body is JSON (see ``append_channel_delegation``).
CHANNEL_DELEGATION_SENDER = "ChannelDelegation"

# Every busy-state label variant so the status chip width stays fixed (no header jump).
_STATUS_CHIP_TEXTS = (
    "Ready",
    "Working",
    "Working.",
    "Working..",
    "Working...",
)


class _RoundedSurfaceFrame(QFrame):
    """Rounded fill + outline drawn in code — Windows native QStyle ignores QSS radius."""

    def __init__(
        self,
        parent: Optional[QWidget],
        *,
        pill: bool = False,
        corner_radius: float = 12.0,
    ) -> None:
        super().__init__(parent)
        self._pill = pill
        self._corner_radius = float(corner_radius)
        self._bg = QColor(0, 0, 0, 0)
        self._border = QColor(0, 0, 0, 0)
        self._border_w = 1.0
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAutoFillBackground(False)

    def set_surface(
        self,
        bg: QColor,
        border: QColor,
        border_width: float = 1.0,
    ) -> None:
        self._bg = QColor(bg)
        self._border = QColor(border)
        self._border_w = float(border_width)
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if rect.width() <= 0 or rect.height() <= 0:
            return
        if self._pill:
            ry = rect.height() / 2.0
            rx = ry
        else:
            cap = min(rect.width(), rect.height()) / 2.0
            rx = ry = min(self._corner_radius, cap)
        path = QPainterPath()
        path.addRoundedRect(rect, rx, ry)
        if self._bg.alpha() > 0:
            p.fillPath(path, self._bg)
        if self._border_w > 0 and self._border.alpha() > 0:
            pen = QPen(self._border)
            pen.setWidthF(self._border_w)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.drawPath(path)


class TerminalPopover(QWidget):
    message_sent = pyqtSignal(str)
    closed = pyqtSignal()
    clear_requested = pyqtSignal()
    session_selected = pyqtSignal(str)  # kept for compat; emitted when pin auto-swaps
    reset_anchor_requested = pyqtSignal()
    restart_requested = pyqtSignal()
    use_app_session_requested = pyqtSignal()

    def __init__(self, theme: str = "Peach") -> None:
        super().__init__()
        self.theme = theme
        self.provider_name = "Claude"
        self.character_name = "Bruce"
        self._was_visible = False
        self._anchor_rect = QRect()
        self._taskbar_edge: Optional[int] = None
        self._session_linking_visible = False
        self._linked_input_mode = False

        # Session tracking (no dropdown) — just remembers what the host bound us to.
        self._session_options: List[Tuple[str, str]] = []
        self._selected_session_id: str = "app"
        self._selected_session_label: str = "App session"

        self._assistant_stream_buffer = ""
        self._last_assistant_plain = ""
        self._greeting_shown_for: set = set()
        self._pending_greeting: Optional[tuple] = None
        # (sender, text) log of everything rendered in this session. Kept
        # so ``apply_theme`` can wipe the document and re-render bubbles
        # with the new palette instead of leaving stale inline colors
        # from the previous theme.
        self._message_log: List[Tuple[str, str]] = []

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)

        self._paint_bg = QColor("#FFF7EB")
        self._paint_border = QColor("#F28B9A")
        self._corner_radius = 24.0
        self._border_width = 2.0
        self._provider_accent = QColor(_DEFAULT_VISUAL["accent"])

        self.setObjectName("terminalRoot")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 12)
        outer.setSpacing(0)

        # ---- Header ---------------------------------------------------------
        self.header_bar = QWidget(self)
        self.header_bar.setObjectName("titleBar")
        header_inner = QHBoxLayout(self.header_bar)
        header_inner.setContentsMargins(14, 10, 8, 10)
        header_inner.setSpacing(6)

        # Provider mark — logo from ``assets/pos_logo`` when available, else accent glyph.
        self.provider_dot = QLabel(self.header_bar)
        self.provider_dot.setObjectName("providerDot")
        self.provider_dot.setFixedSize(_HEADER_LOGO_SLOT_W, _HEADER_LOGO_SLOT_H)
        self.provider_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_inner.addWidget(
            self.provider_dot, 0, Qt.AlignmentFlag.AlignVCenter
        )

        # Rich title chip — provider ~ session (auto-bound, no dropdown).
        self.title = QLabel(self.header_bar)
        self.title.setObjectName("titleLabel")
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        header_inner.addWidget(self.title, 1, Qt.AlignmentFlag.AlignVCenter)

        # Status pill — custom paint (native Windows QStyle ignores QSS border-radius).
        self.status_chip = _RoundedSurfaceFrame(self.header_bar, pill=True)
        _status_lay = QHBoxLayout(self.status_chip)
        _status_lay.setContentsMargins(12, 5, 12, 5)
        _status_lay.setSpacing(0)
        self.status = QLabel("Ready", self.status_chip)
        self.status.setObjectName("statusLabel")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        _status_lay.addWidget(self.status)
        header_inner.addWidget(self.status_chip, 0, Qt.AlignmentFlag.AlignVCenter)

        self.close_btn = QPushButton("\u2715", self.header_bar)  # unicode ✕
        self.close_btn.setObjectName("closeButton")
        self.close_btn.setFixedSize(26, 26)
        self.close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_btn.setToolTip("Hide chat (Esc)")
        self.close_btn.clicked.connect(self.hide)
        header_inner.addWidget(self.close_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._busy = False
        self._busy_phase = 0
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(360)
        self._busy_timer.timeout.connect(self._tick_busy)

        outer.addWidget(self.header_bar)

        # ---- Link-sync banner ----------------------------------------------
        self._sync_banner = _RoundedSurfaceFrame(self, pill=False, corner_radius=12.0)
        self._sync_banner.setObjectName("linkSyncBanner")
        _sync_lay = QVBoxLayout(self._sync_banner)
        _sync_lay.setContentsMargins(0, 0, 0, 0)
        self._sync_banner_label = QLabel(self._sync_banner)
        self._sync_banner_label.setObjectName("linkSyncBannerLabel")
        self._sync_banner_label.setWordWrap(True)
        self._sync_banner_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.NoTextInteraction
        )
        _sync_lay.addWidget(self._sync_banner_label)
        self._sync_banner.hide()
        outer.addWidget(self._sync_banner)

        # ---- Body -----------------------------------------------------------
        body_layout = QVBoxLayout()
        body_layout.setContentsMargins(6, 8, 6, 8)
        body_layout.setSpacing(10)

        self.chat_display = QTextEdit(self)
        self.chat_display.setReadOnly(True)
        self.chat_display.setAcceptRichText(True)
        self.chat_display.setFont(QFont("Segoe UI", 11))
        self.chat_display.setObjectName("chatDisplay")
        self.chat_display.setFrameShape(QFrame.Shape.NoFrame)
        self.chat_display.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.chat_display.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.chat_display.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._apply_chat_stylesheet()
        body_layout.addWidget(self.chat_display, 1)

        self.input_field = QLineEdit(self)
        self.input_field.setObjectName("inputField")
        self.input_field.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.input_field.setFont(QFont("Segoe UI", 11))
        self.input_field.returnPressed.connect(self.send_message)
        self._completion_model = QStringListModel(self)
        self._completer = QCompleter(self)
        self._completer.setModel(self._completion_model)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchFlag.MatchStartsWith)
        self._completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._completer.setMaxVisibleItems(14)
        self.input_field.setCompleter(self._completer)
        body_layout.addWidget(self.input_field)

        outer.addLayout(body_layout, 1)

        self.apply_theme(theme)
        self.resize(460, 380)
        self.setMinimumSize(400, 300)
        self.update_header()
        self._update_default_placeholder()
        self._update_input_hint()

    # ------------------------------------------------------------------ theme

    def _theme_tokens(self, theme: str) -> dict:
        presets = {
            "Peach": {
                "popover_bg": "#FFF4E6",
                "popover_border": "#F28B9A",
                "title_bar_bg": "#FAEDE0",
                "title_text": "#8B3D4A",
                "body_text": "#2E2430",
                "text_dim": "#7A6B73",
                "accent": "#D94D6A",
                "error": "#E64D3D",
                "success": "#3A9B6E",
                "chat_bg": "#FFEFDC",
                "input_bg": "#FFFAF1",
                "bubble_user": "#FFEFDC",
                "bubble_assistant": "#FFEFDC",
                "bubble_system": "#FFEFDC",
                "bubble_user_header": "#FFCDB4",
                "bubble_assistant_header": "#FFE8D0",
                "bubble_user_border": "#E5B088",
                "bubble_assistant_border": "#E8C6A6",
                "code_bg": "#FFEBD3",
                "code_border": "#E8A992",
                "radius": 24,
            },
            "Midnight": {
                "popover_bg": "#12121A",
                "popover_border": "#FF6600",
                "title_bar_bg": "#1A1A22",
                "title_text": "#FF9A33",
                "body_text": "#ECECF4",
                "text_dim": "#9898A8",
                "accent": "#FF6600",
                "error": "#FF5544",
                "success": "#66BB6A",
                "chat_bg": "#15151D",
                "input_bg": "#1E1E28",
                "bubble_user": "#15151D",
                "bubble_assistant": "#15151D",
                "bubble_system": "#15151D",
                "bubble_user_header": "#343A4E",
                "bubble_assistant_header": "#272C3C",
                "bubble_user_border": "#495470",
                "bubble_assistant_border": "#3B455D",
                "code_bg": "#0C0C12",
                "code_border": "#3A3A46",
                "radius": 22,
            },
            "Cloud": {
                "popover_bg": "#EEF1F6",
                "popover_border": "#C8CED6",
                "title_bar_bg": "#E0E4EA",
                "title_text": "#3D4450",
                "body_text": "#1F232B",
                "text_dim": "#6B7280",
                "accent": "#0078D4",
                "error": "#D93025",
                "success": "#2E9E4B",
                "chat_bg": "#F4F6FB",
                "input_bg": "#FFFFFF",
                "bubble_user": "#F4F6FB",
                "bubble_assistant": "#F4F6FB",
                "bubble_system": "#F4F6FB",
                "bubble_user_header": "#CDDDF0",
                "bubble_assistant_header": "#EEF2F9",
                "bubble_user_border": "#B8CEE6",
                "bubble_assistant_border": "#CBD5E3",
                "code_bg": "#F1F4F9",
                "code_border": "#C5CCD4",
                "radius": 23,
            },
            "Moss": {
                "popover_bg": "#EFF5ED",
                "popover_border": "#7CB342",
                "title_bar_bg": "#E4EFDE",
                "title_text": "#33691E",
                "body_text": "#1F2A23",
                "text_dim": "#5D6B5F",
                "accent": "#558B2F",
                "error": "#C62828",
                "success": "#43A047",
                "chat_bg": "#F1F7EC",
                "input_bg": "#FCFEFB",
                "bubble_user": "#F1F7EC",
                "bubble_assistant": "#F1F7EC",
                "bubble_system": "#F1F7EC",
                "bubble_user_header": "#CFE2BB",
                "bubble_assistant_header": "#EAF3E2",
                "bubble_user_border": "#B4C995",
                "bubble_assistant_border": "#C8D9B9",
                "code_bg": "#EBF3E2",
                "code_border": "#BED3A9",
                "radius": 23,
            },
        }
        return presets.get(theme, presets["Peach"])

    def _apply_chat_stylesheet(self) -> None:
        c = self._theme_tokens(self.theme)
        self.chat_display.document().setDocumentMargin(4)
        self.chat_display.document().setDefaultStyleSheet(f"""
            body {{
                font-size: 14px;
                line-height: 1.42;
                font-family: "Segoe UI Variable", "Segoe UI", Arial;
                color: {c['body_text']};
                background: transparent;
            }}
            h1 {{ font-size: 17px; margin: 8px 0 4px 0; color: {c['body_text']}; }}
            h2 {{ font-size: 15px; margin: 7px 0 4px 0; color: {c['body_text']}; }}
            h3 {{ font-size: 14px; margin: 6px 0 3px 0; color: {c['body_text']}; }}
            h4, h5, h6 {{ font-size: 13px; margin: 5px 0 3px 0; color: {c['body_text']}; }}
            p {{ margin: 0 0 5px 0; }}
            p:last-child {{ margin-bottom: 0; }}
            ul {{ margin: 4px 0 6px 0; padding-left: 1.2em; }}
            ol {{ margin: 4px 0 6px 0; padding-left: 1.4em; }}
            li {{ margin-bottom: 2px; }}
            li::marker {{ color: {c['accent']}; }}
            pre {{
                background: {c['code_bg']};
                border-left: 3px solid {c['accent']};
                padding: 8px 10px;
                border-radius: 10px;
                margin: 6px 0;
                font-family: "Cascadia Code", "Cascadia Mono", "Consolas", monospace;
                font-size: 12px;
            }}
            code {{
                background: {c['code_bg']};
                border: 1px solid {c['code_border']};
                border-radius: 5px;
                padding: 1px 5px;
                font-family: "Cascadia Code", "Cascadia Mono", "Consolas", monospace;
                font-size: 12px;
            }}
            blockquote {{
                border-left: 3px solid {c['text_dim']};
                padding: 2px 10px;
                margin: 6px 0;
                color: {c['text_dim']};
            }}
            a {{ color: {c['accent']}; text-decoration: none; }}
            strong {{ color: {c['body_text']}; font-weight: 700; }}
            hr {{ border: 0; border-top: 1px solid rgba(0,0,0,0.08); margin: 10px 0; }}
        """)

    def apply_theme(self, theme: str) -> None:
        previous_theme = self.theme
        self.theme = theme
        c = self._theme_tokens(theme)
        r = int(c["radius"])
        self._paint_bg = QColor(c["popover_bg"])
        self._paint_border = QColor(c["popover_border"])
        self._corner_radius = float(r)
        r_header = max(8, r - 6)
        r_input = max(14, min(20, r - 4))

        visual = _visual_for(self.provider_name)
        acc_hex = visual["accent"]
        ar, ag, ab = _hex_to_rgb(acc_hex)
        tr, tg, tb = _hex_to_rgb(c["title_text"])
        is_dark = theme == "Midnight"
        dim_rgba = "rgba(255,255,255,0.06)" if is_dark else "rgba(0,0,0,0.05)"
        divider_rgba = "rgba(255,255,255,0.08)" if is_dark else "rgba(0,0,0,0.06)"
        input_border_rgba = (
            "rgba(255,255,255,0.10)" if is_dark else "rgba(0,0,0,0.09)"
        )
        scroll_thumb_rgba = (
            "rgba(255,255,255,0.22)" if is_dark else "rgba(0,0,0,0.20)"
        )
        close_hover_rgba = f"rgba({ar},{ag},{ab},0.15)"
        status_bg_a = int(round(0.14 * 255))
        status_outline_a = int(round(0.22 * 255))
        banner_bg_a = int(round(0.10 * 255))
        banner_outline_a = int(round(0.35 * 255))

        self.setStyleSheet(f"""
            QWidget#terminalRoot {{
                background-color: transparent;
                color: {c['body_text']};
                border: none;
            }}
            QWidget#titleBar {{
                background-color: transparent;
                border: none;
                border-bottom: 1px solid {divider_rgba};
            }}
            QLabel#providerDot {{
                background: transparent;
                border: none;
                font-size: 12px;
                font-weight: 900;
                padding: 0;
                color: {acc_hex};
            }}
            QLabel#titleLabel {{
                background: transparent;
                border: none;
                color: {c['title_text']};
                font-size: 13px;
                font-weight: 800;
                padding: 0 2px;
            }}
            QLabel#statusLabel {{
                background: transparent;
                border: none;
                color: {acc_hex};
                font-size: 11px;
                font-weight: 800;
                letter-spacing: 0.28px;
                padding: 0;
            }}
            QPushButton#closeButton {{
                background: transparent;
                border: none;
                color: {c['text_dim']};
                font-size: 13px;
                font-weight: 500;
                border-radius: 13px;
                padding: 0;
            }}
            QPushButton#closeButton:hover {{
                background: {close_hover_rgba};
                color: {acc_hex};
            }}
            QPushButton#closeButton:pressed {{
                background: rgba({ar},{ag},{ab},0.28);
            }}
            QTextEdit#chatDisplay {{
                background-color: {c['chat_bg']};
                border: none;
                border-radius: 16px;
                padding: 10px 6px 10px 10px;
                color: {c['body_text']};
                selection-background-color: {acc_hex};
                selection-color: white;
            }}
            QLineEdit#inputField {{
                background-color: {c['input_bg']};
                border: 1.5px solid {input_border_rgba};
                border-radius: {r_input}px;
                padding: 11px 16px;
                color: {c['body_text']};
                font-size: 14px;
                font-weight: 400;
                selection-background-color: {acc_hex};
                selection-color: white;
            }}
            QLineEdit#inputField:focus {{
                background-color: {c['input_bg']};
                border: 1.5px solid {acc_hex};
            }}
            QLineEdit#inputField:disabled {{
                color: {c['text_dim']};
                background-color: {dim_rgba};
            }}
            QLabel#linkSyncBannerLabel {{
                background: transparent;
                border: none;
                color: {c['body_text']};
                font-size: 11.5px;
                font-weight: 500;
                padding: 8px 12px;
            }}
            QScrollBar:vertical {{
                background: transparent;
                width: 8px;
                margin: 6px 2px 6px 0;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {scroll_thumb_rgba};
                border-radius: 4px;
                min-height: 36px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: rgba({ar},{ag},{ab},0.55);
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; background: none; border: none;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
            QScrollBar:horizontal {{ height: 0; background: transparent; }}
            QCompleter QAbstractItemView {{
                background-color: {c['input_bg']};
                color: {c['body_text']};
                border: 1px solid {divider_rgba};
                border-radius: 10px;
                padding: 4px;
                outline: 0;
                selection-background-color: rgba({ar},{ag},{ab},0.18);
                selection-color: {c['body_text']};
            }}
        """)
        self.status_chip.set_surface(
            QColor(ar, ag, ab, status_bg_a),
            QColor(ar, ag, ab, status_outline_a),
            1.0,
        )
        self._sync_banner.set_surface(
            QColor(ar, ag, ab, banner_bg_a),
            QColor(ar, ag, ab, banner_outline_a),
            1.0,
        )
        self._refit_status_chip_fixed_width()
        self._apply_chat_stylesheet()
        self.update()

        # If the theme actually changed and there's prior chat content,
        # re-render every message so bubbles pick up the new palette
        # instead of keeping their baked-in inline colors.
        if previous_theme != theme and self._message_log:
            log = list(self._message_log)
            self.set_messages(log)

    def _refit_status_chip_fixed_width(self) -> None:
        """Keep status pill width constant so Working/… does not resize the header."""
        lay = self.status_chip.layout()
        ml = mt = mr = mb = 0
        if lay is not None:
            ml, mt, mr, mb = lay.getContentsMargins()
        fm = QFontMetrics(self.status.font())
        text_w = max(fm.horizontalAdvance(t) for t in _STATUS_CHIP_TEXTS)
        chip_w = int(math.ceil(text_w + ml + mr + 2))
        self.status_chip.setFixedWidth(max(chip_w, 72))

    def _append_html_block(self, html: str) -> None:
        """Append one fully-styled HTML card to the chat document.

        QTextDocument copies the *block format* of an HTML `<div>` with a
        background color into the next empty block created by
        ``insertBlock()``. That's what produced the phantom colored
        rectangle under each bubble. We terminate every card with an
        explicitly empty ``QTextBlockFormat`` / ``QTextCharFormat`` so the
        following paragraph is rendered neutrally.
        """
        cursor = self.chat_display.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        if not self.chat_display.document().isEmpty():
            cursor.insertHtml(
                "<div style='height:10px;line-height:10px;'>&nbsp;</div>"
            )
        cursor.insertHtml(html)
        cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
        self.chat_display.setTextCursor(cursor)
        self._scroll_to_bottom()

    def _message_card_html(
        self,
        *,
        sender_label: str,
        body_html: str,
        body_bg: str,
        border_color: str,
        label_color: str,
        align_right: bool = False,
    ) -> str:
        """Render one chat message on the same visual surface as the theme.

        The goal is to avoid a second "punchy" background behind the text.
        Messages use the chat surface color and rely on typography plus
        border tint for identity.
        """
        c = self._theme_tokens(self.theme)
        body_color = c["body_text"]
        if align_right:
            table_align = "right"
            wrapper_margin = "margin:3px 8px 3px 84px;"
            label_style = "padding:0 2px 4px 0;text-align:right;"
            card_style = "display:inline-block;max-width:320px;text-align:right;"
            bubble_style = (
                f"background:{body_bg};border:1px solid {border_color};"
                f"border-radius:16px 16px 6px 16px;padding:8px 12px;"
                f"color:{body_color};text-align:right;"
            )
        else:
            table_align = "left"
            wrapper_margin = "margin:3px 84px 3px 2px;"
            label_style = "padding:0 0 4px 2px;text-align:left;"
            card_style = "display:inline-block;max-width:320px;text-align:left;"
            bubble_style = (
                f"background:{body_bg};border:1px solid {border_color};"
                f"border-radius:16px 16px 16px 6px;padding:8px 12px;"
                f"color:{body_color};text-align:left;"
            )

        return (
            f"<table width='100%' cellspacing='0' cellpadding='0' "
            f"style='border-collapse:collapse;{wrapper_margin}'>"
            f"<tr><td align='{table_align}'>"
            f"<div style='{card_style}'>"
            f"<div style='{label_style}font-size:12px;font-weight:700;"
            f"color:{label_color};'>{escape(sender_label)}</div>"
            f"<div style='{bubble_style}'>{body_html}</div>"
            f"</div></td></tr></table>"
        )

    def _channel_delegation_card_html(
        self,
        *,
        ui_user: str,
        from_character: str,
        to_character: str,
        body_plain: str,
    ) -> str:
        """Inbound task from another walker — Admin + who → who + body (right-aligned)."""
        c = self._theme_tokens(self.theme)
        visual = _visual_for(self.provider_name)
        acc = visual["accent"]
        ar, ag, ab = _hex_to_rgb(acc)
        body_color = c["body_text"]
        body_html = _escape_text_with_breaks(body_plain)
        ribbon_bg = f"rgba({ar},{ag},{ab},0.14)"
        ribbon_border = f"rgba({ar},{ag},{ab},0.4)"
        u = escape((ui_user or _current_ui_user_name()).strip() or "You")
        fc = escape((from_character or "").strip() or "?")
        tc = escape((to_character or "").strip() or "?")
        route = f"{fc} → {tc}"
        bubble_bg = c["bubble_user"]
        bubble_border = c["bubble_user_border"]
        wrapper_margin = "margin:4px 8px 3px 72px;"
        return (
            f"<table width='100%' cellspacing='0' cellpadding='0' "
            f"style='border-collapse:collapse;{wrapper_margin}'>"
            f"<tr><td align='right'>"
            f"<div style='display:inline-block;max-width:320px;text-align:right;'>"
            f"<div style='font-size:12.5px;font-weight:800;color:{c['title_text']};"
            f"letter-spacing:0.01em;padding:0 2px 3px 0;'>{u}</div>"
            f"<div style='display:inline-block;padding:3px 10px;border-radius:999px;"
            f"font-size:11px;font-weight:800;letter-spacing:0.03em;color:{acc};"
            f"background:{ribbon_bg};border:1px solid {ribbon_border};'>{route}</div>"
            f"<div style='margin-top:7px;background:{bubble_bg};"
            f"border:1px solid {bubble_border};border-left:3px solid {acc};"
            f"border-radius:16px 16px 6px 16px;padding:9px 13px;"
            f"color:{body_color};text-align:left;font-size:14px;line-height:1.42;'>"
            f"{body_html}</div>"
            f"</div></td></tr></table>"
        )

    def append_channel_delegation(
        self,
        *,
        from_character: str,
        to_character: str,
        body: str,
        ui_user: Optional[str] = None,
    ) -> None:
        """Show a channel /tell handoff with clear Admin + source → target layout."""
        msg = (body or "").strip()
        if not msg:
            return
        payload = json.dumps(
            {
                "v": 1,
                "u": (ui_user or _current_ui_user_name()).strip() or "You",
                "f": (from_character or "").strip(),
                "t": (to_character or "").strip(),
                "b": msg,
            },
            ensure_ascii=False,
        )
        self.append_message(CHANNEL_DELEGATION_SENDER, payload)

    def _character_display_name(self) -> str:
        raw = (self.character_name or "").replace("_", " ").strip()
        return raw.title() or "Assistant"

    def _message_sender_identity(self, sender: str) -> tuple[str, str]:
        if sender == "User":
            return _current_ui_user_name(), ""
        if sender == self.provider_name:
            return self._character_display_name(), ""
        if sender == "Tool":
            return "Tool", ""
        if sender == "Error":
            return "Error", ""
        return sender, ""

    # ------------------------------------------------------------------ paint

    def _rounded_path(self) -> QPainterPath:
        path = QPainterPath()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path.addRoundedRect(rect, self._corner_radius, self._corner_radius)
        return path

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        path = self._rounded_path()
        p.fillPath(path, self._paint_bg)
        pen = QPen(self._paint_border)
        pen.setWidthF(float(self._border_width))
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawPath(path)

        # A soft inner shadow of the provider accent for extra identity flair.
        accent_pen = QPen(self._provider_accent)
        accent_pen.setWidthF(max(1.0, self._border_width - 1.0))
        accent_pen.setStyle(Qt.PenStyle.DotLine)
        accent_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        # We don't draw the inner stroke here to keep the chrome quiet — the
        # outer border width and the header dot already telegraph the provider.

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)

    # ------------------------------------------------------------------ state

    def get_persisted_ui_state(self) -> dict:
        return {
            "messages": list(self._message_log),
            "last_assistant_plain": self._last_assistant_plain,
            "assistant_stream_buffer": self._assistant_stream_buffer,
            "input_placeholder": self.input_field.placeholderText(),
        }

    def apply_persisted_ui_state(self, state: Optional[dict]) -> None:
        if not state:
            self.reset_chat()
            return
        messages = state.get("messages")
        if isinstance(messages, list):
            # Re-render from the structured log so bubbles use the current
            # theme's colors even if the popover was saved under another.
            self.set_messages(
                [(str(s), str(t)) for s, t in messages if isinstance(s, str)]
            )
        else:
            # Legacy persisted state carried raw HTML; fall back to that
            # when the log isn't available so nothing is lost on upgrade.
            legacy_html = state.get("chat_html", "")
            self.chat_display.setHtml(str(legacy_html))
            self._message_log = []
        self._last_assistant_plain = state.get("last_assistant_plain", "")
        self._assistant_stream_buffer = state.get("assistant_stream_buffer", "")
        ph = state.get("input_placeholder")
        if isinstance(ph, str) and ph.strip():
            self.input_field.setPlaceholderText(ph)
        sb = self.chat_display.verticalScrollBar()
        sb.setValue(sb.maximum())

    # Backward-compat: host toggles whether linking UI is shown. We no longer
    # render a dropdown; the rich title chip (updated in update_header) carries
    # the active session label instead.
    def set_session_linking_visible(self, visible: bool) -> None:
        self._session_linking_visible = bool(visible)
        if not self._session_linking_visible:
            self.set_link_sync_banner(None)
            self.set_linked_input_mode(False)
        self.update_header()

    def set_linked_input_mode(self, linked: bool) -> None:
        self._linked_input_mode = bool(linked)

    def set_link_sync_banner(self, text: Optional[str]) -> None:
        if not text or not str(text).strip():
            self._sync_banner.hide()
            self._sync_banner_label.clear()
            return
        self._sync_banner_label.setText(str(text).strip())
        self._sync_banner.show()

    def _update_input_hint(self) -> None:
        self.input_field.setToolTip(
            f"Sent to your installed {self.provider_name} CLI (same as a terminal). "
            "Type / or -- then use arrow keys or Tab to pick hints from the real CLI --help. "
            "In-app: /help /clear /copy /resetpos. Linked: CLI /commands pass through; "
            "//clear //copy //help stay local; Enter paste+submit when bridge is on. "
            "Extra walker commands: /provider /spawn /channel /tell /collab."
        )

    def set_completion_strings(self, strings: object) -> None:
        if not isinstance(strings, list):
            return
        self._completion_model.setStringList([str(s) for s in strings if str(s).strip()])

    def _update_default_placeholder(self) -> None:
        user_name = _current_ui_user_name()
        self.input_field.setPlaceholderText(
            f"Describe a task or ask a question"
        )

    def _apply_provider_header_mark(self) -> None:
        """Show the per-provider PNG logo when present; otherwise the Unicode dot."""
        pm = _provider_logo_pixmap(self.provider_name)
        if pm and not pm.isNull():
            self.provider_dot.clear()
            self.provider_dot.setPixmap(pm)
            self.provider_dot.setText("")
            self.provider_dot.setFixedSize(_HEADER_LOGO_SLOT_W, _HEADER_LOGO_SLOT_H)
            self.provider_dot.setScaledContents(False)
        else:
            self.provider_dot.clear()
            self.provider_dot.setPixmap(QPixmap())
            visual = _visual_for(self.provider_name)
            self.provider_dot.setText(visual["dot"])
            self.provider_dot.setFixedSize(_HEADER_LOGO_SLOT_W, _HEADER_LOGO_SLOT_H)

    def update_header(self) -> None:
        self._apply_provider_header_mark()
        # Re-apply stylesheet so providerDot color updates with provider
        self.apply_theme(self.theme) if False else None  # keep idempotent
        char_label = self._character_display_name()
        # Title chip — provider + character, then session binding.
        if self._selected_session_id and self._selected_session_id != "app":
            label = self._selected_session_label or "linked terminal"
            chip = (
                f"{escape(self.provider_name)}"
                f" <span style='opacity:0.5;'>·</span> "
                f"<span style='font-weight:700;'>{escape(char_label)}</span>"
                f" <span style='opacity:0.5;'>·</span> "
                f"<span style='font-weight:600;opacity:0.85;'>{escape(label)}</span>"
            )
        else:
            chip = (
                f"{escape(self.provider_name)}"
                f" <span style='opacity:0.5;'>·</span> "
                f"<span style='font-weight:700;'>{escape(char_label)}</span>"
                f" <span style='opacity:0.65;'>· app session</span>"
            )
        self.title.setText(f"<span>{chip}</span>")

    def set_context(self, provider_name: str, character_name: str) -> None:
        self.provider_name = provider_name
        self.character_name = character_name
        # Refresh provider-scoped styling (dot color, focus border, accent inner)
        self._provider_accent = QColor(_visual_for(provider_name)["accent"])
        self._border_width = float(_visual_for(provider_name)["border_width"])
        self.apply_theme(self.theme)
        self.update_header()
        ph = self.input_field.placeholderText()
        if not ph.startswith("Bridge:"):
            self._update_default_placeholder()
        self._update_input_hint()
        self.update()

    def set_greeting(self, display_name: str, greeting: str) -> None:
        """Queue a welcome line from the character.

        The caller is responsible for resolving any user-name substitution
        (`{user}`) in ``greeting`` beforehand — this keeps the popover free
        of OS-specific env lookups. We render the greeting as the character
        speaking (assistant bubble), so it reads like "Bruce → Hey Admin…"
        instead of the old, awkward "Jazz: Hey, Jazz…" system line.
        """
        key = (self.provider_name, self.character_name, (greeting or "").strip())
        if not greeting or key in self._greeting_shown_for:
            self._pending_greeting = None
            return
        self._pending_greeting = (key, display_name, greeting)

    def _flush_pending_greeting(self) -> None:
        pending = self._pending_greeting
        if not pending:
            return
        key, display_name, greeting = pending
        if key in self._greeting_shown_for:
            self._pending_greeting = None
            return
        self.append_message(display_name or self.character_name.title(), greeting)
        self._greeting_shown_for.add(key)
        self._pending_greeting = None

    # ------------------------------------------------------------------ busy

    def set_busy_state(self, busy: bool) -> None:
        self._busy = bool(busy)
        if self._busy:
            self._busy_phase = 0
            self.status.setText("Working")
            if not self._busy_timer.isActive():
                self._busy_timer.start()
        else:
            if self._busy_timer.isActive():
                self._busy_timer.stop()
            self.status.setText("Ready")

    def _tick_busy(self) -> None:
        if not self._busy:
            return
        dots = "." * ((self._busy_phase % 3) + 1)
        self.status.setText(f"Working{dots}")
        self._busy_phase += 1

    def set_input_enabled(self, enabled: bool, placeholder: Optional[str] = None) -> None:
        self.input_field.setEnabled(enabled)
        if placeholder is not None:
            self.input_field.setPlaceholderText(placeholder)
        elif enabled:
            self._update_default_placeholder()

    def reset_chat(self) -> None:
        self.chat_display.clear()
        self._assistant_stream_buffer = ""
        self._last_assistant_plain = ""
        self._message_log = []

    def set_messages(self, messages) -> None:
        self.chat_display.clear()
        self._assistant_stream_buffer = ""
        self._last_assistant_plain = ""
        # ``append_message`` refills ``_message_log`` for each entry below,
        # so we clear it here to avoid double-logging.
        self._message_log = []
        for sender, text in messages:
            self.append_message(sender, text)

    # Compat — legacy callers still wire session options through here. We just
    # remember the selected label for the title chip; no dropdown is rendered.
    def set_session_options(self, options, selected_id: str) -> None:
        self._session_options = [(str(sid), str(label)) for sid, label in options]
        self._selected_session_id = str(selected_id or "app")
        label = next((lab for sid, lab in self._session_options if sid == self._selected_session_id), "")
        # Trim "— Claude [command]" etc suffix noise; keep the short host part.
        short = label
        if "—" in label:
            short = label.split("—", 1)[0].strip()
        elif " · " in label:
            short = label.split(" · ", 1)[0].strip()
        self._selected_session_label = short or label or "linked"
        self.update_header()

    def commit_assistant_turn(self) -> None:
        buf = self._assistant_stream_buffer.strip()
        if buf:
            self._last_assistant_plain = buf

    def _clear_slash_buffers(self) -> None:
        self._assistant_stream_buffer = ""
        self._last_assistant_plain = ""

    def _append_system_line(self, text: str, *, error: bool = False) -> None:
        c = self._theme_tokens(self.theme)
        color = c["error"] if error else c["text_dim"]
        html = (
            f"<div style='text-align:center;margin:4px 40px;'>"
            f"<span style='display:inline-block;font-size:11px;color:{color};"
            f"padding:2px 8px;font-style:italic;'>"
            f"{escape(text)}</span></div>"
        )
        self._append_html_block(html)

    def _scroll_to_bottom(self) -> None:
        sb = self.chat_display.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ------------------------------------------------------------------ slash

    def _handle_slash_command(self, text: str) -> bool:
        if not text.startswith("/"):
            return False
        raw = text.strip()
        cmd = raw.lower()

        if self._linked_input_mode:
            if cmd == "/resetpos":
                self.reset_anchor_requested.emit()
                self._append_system_line("Chat window snapped next to the walker; it will follow the character again.")
                return True
            if cmd == "//clear":
                self._clear_slash_buffers()
                self.clear_requested.emit()
                return True
            if cmd == "//copy":
                return self._slash_copy()
            if cmd == "//help":
                return self._slash_help_linked()
            # // prefix is the app-local escape hatch in linked mode — keep it
            # working for /restart and /session so the user can escape a bad
            # paste-bridge binding without digging into Settings.
            if cmd in ("//restart", "//session"):
                return self._handle_local_admin(cmd.lstrip("/"))
            return False

        if cmd == "/clear":
            self._clear_slash_buffers()
            self.clear_requested.emit()
            return True

        if cmd == "/copy":
            return self._slash_copy()

        if cmd == "/help":
            return self._slash_help_app()

        if cmd == "/resetpos":
            self.reset_anchor_requested.emit()
            self._append_system_line("Chat window snapped next to the walker; it will follow the character again.")
            return True

        if cmd in ("/restart", "/session"):
            return self._handle_local_admin(cmd.lstrip("/"))

        return False

    def _handle_local_admin(self, action: str) -> bool:
        """Handle the /restart and /session meta-commands consistently."""
        if action == "restart":
            self._clear_slash_buffers()
            self.restart_requested.emit()
            self._append_system_line("Restarting provider session…")
            return True
        if action == "session":
            self.use_app_session_requested.emit()
            self._append_system_line(
                "Switched to the in-app session."
            )
            return True
        return False

    def _slash_copy(self) -> bool:
        clip = QGuiApplication.clipboard()
        to_copy = self._last_assistant_plain or ""
        if not to_copy.strip():
            self._append_system_line("nothing to copy yet")
        else:
            clip.setText(to_copy)
            # Use the neutral system-line style instead of the old green badge
            # — feedback is visible without the jarring green-on-dark contrast.
            self._append_system_line("copied to clipboard")
        return True

    def _slash_help_app(self) -> bool:
        c = self._theme_tokens(self.theme)
        dim = c["text_dim"]
        acc = _visual_for(self.provider_name)["accent"]
        body = c["body_text"]

        def row(name: str, desc: str) -> str:
            return (
                f"<div style='display:flex;gap:10px;padding:1px 0;'>"
                f"<b style='min-width:74px;color:{body};'>{name}</b>"
                f"<span style='color:{dim};'>{desc}</span></div>"
            )

        sections = (
            f"<div style='font-weight:700;color:{acc};margin-bottom:4px;'>"
            f"LilWin-Bros — popover commands</div>"
            + row("/help", "show this message")
            + row("/clear", "clear chat &amp; session state")
            + row("/copy", "copy last assistant reply")
            + row("/restart", "kill and respawn the provider CLI")
            + row("/resetpos", "snap chat next to the walker")
            + f"<div style='color:{acc};margin:6px 0 2px;font-size:11px;"
              f"text-transform:uppercase;letter-spacing:.5px;'>Shell</div>"
            + row("/pwd", "print current working directory")
            + row("/cd &lt;dir&gt;", "change cwd (CLI session restarts there)")
            + row("/ls [dir]", "list files in a directory")
            + row("/run &lt;cmd&gt;", "run a shell command here (30s timeout)")
            + f"<div style='color:{acc};margin:6px 0 2px;font-size:11px;"
              f"text-transform:uppercase;letter-spacing:.5px;'>Sessions</div>"
            + row("/link", "list available external terminal sessions")
            + row("/link &lt;N&gt;", "link popover to terminal #N")
            + row("/unlink", "drop the link, go back to app session")
            + row("/session", "same as /unlink (legacy)")
            + row("/who", "show provider, character, cwd, link state")
            + f"<div style='color:{acc};margin:6px 0 2px;font-size:11px;"
              f"text-transform:uppercase;letter-spacing:.5px;'>Cosmetics</div>"
            + row("/theme &lt;name&gt;", "Peach, Midnight, Cloud, Moss")
            + row("/size &lt;n&gt;", "small | medium | large")
            + row("/spawn &lt;char&gt; [provider]", "stand up another walker with an optional provider")
            + row("/provider [name]", "show or switch this walker's provider only")
            + row("/channel ...", "create/join/leave/list a shared walker channel")
            + row("/tell &lt;char&gt; &lt;msg&gt;", "send one line only to that walker's provider")
            + row("/collab ...", "pair for handoff turns, or on/off/clear")
            + f"<div style='margin-top:6px;color:{dim};'>"
              f"Everything else is sent to your <b>{escape(self.provider_name)}"
              f"</b> CLI. In <b>linked</b> mode, prefix admin commands with "
              f"<b>//</b> (e.g. <code>//help</code>) so they hit the popover "
              f"instead of the terminal.</div>"
        )
        help_html = f"<div style='margin:8px 0;font-size:12px;color:{body};'>{sections}</div>"
        self.chat_display.append(help_html)
        self._scroll_to_bottom()
        return True

    def _slash_help_linked(self) -> bool:
        c = self._theme_tokens(self.theme)
        dim = c["text_dim"]
        acc = _visual_for(self.provider_name)["accent"]
        body = c["body_text"]
        help_html = (
            f"<div style='margin:8px 0;font-size:12px;color:{body};'>"
            f"<div style='font-weight:700;color:{acc};'>LilWin-Bros — linked mode</div>"
            f"<div><b>/resetpos</b> <span style='color:{dim};'>snap chat next to the walker</span></div>"
            f"<div><b>//clear</b> <span style='color:{dim};'>clear this popover &amp; mirror state</span></div>"
            f"<div><b>//copy</b> <span style='color:{dim};'>copy last mirrored assistant text</span></div>"
            f"<div><b>//restart</b> <span style='color:{dim};'>restart the underlying provider session</span></div>"
            f"<div><b>//session</b> <span style='color:{dim};'>drop the linked terminal — use app-managed session</span></div>"
            f"<div><b>//help</b> <span style='color:{dim};'>this message</span></div>"
            f"<div style='margin-top:6px;color:{dim};'>Any other line (including <b>/commands</b> for "
            f"<b>{escape(self.provider_name)}</b>) is sent to the linked terminal: paste + Enter when auto-bridge is on.</div>"
            f"</div>"
        )
        self.chat_display.append(help_html)
        self._scroll_to_bottom()
        return True

    # ------------------------------------------------------------------ send

    def send_message(self) -> None:
        text = self.input_field.text().strip()
        if not text:
            return
        if self._handle_slash_command(text):
            self.input_field.clear()
            return
        self._assistant_stream_buffer = ""
        self.append_message("User", text)
        self.message_sent.emit(text)
        self.input_field.clear()

    def append_message(self, sender: str, text: str) -> None:
        if sender == CHANNEL_DELEGATION_SENDER:
            raw = text or ""
            try:
                o = json.loads(raw)
                if int(o.get("v") or 0) != 1:
                    raise ValueError("version")
                body = (o.get("b") or "").strip()
                if not body:
                    return
            except (json.JSONDecodeError, TypeError, ValueError):
                self._message_log.append(("System", "Malformed channel delegation."))
                self._append_system_line("Malformed channel delegation.")
                return
            self._message_log.append((sender, raw))
            html = self._channel_delegation_card_html(
                ui_user=str(o.get("u") or ""),
                from_character=str(o.get("f") or ""),
                to_character=str(o.get("t") or ""),
                body_plain=body,
            )
            self._append_html_block(html)
            return

        # Skip blank chunks so assistant streams that emit empty tokens
        # don't render as stray empty bubbles underneath a real reply.
        if text is None or not str(text).strip():
            if sender == self.provider_name and text:
                self._assistant_stream_buffer += text
            return

        self._message_log.append((sender, text))

        if sender == self.provider_name:
            self._assistant_stream_buffer += text

        c = self._theme_tokens(self.theme)
        acc = _visual_for(self.provider_name)["accent"]

        if sender == "User":
            label, _meta = self._message_sender_identity(sender)
            html = self._message_card_html(
                sender_label=label,
                body_html=_escape_text_with_breaks(text),
                body_bg=c["bubble_user"],
                border_color=c["bubble_user_border"],
                label_color=c["title_text"],
                align_right=True,
            )
            self._append_html_block(html)
            return

        if sender == "System":
            self._append_system_line(text)
            return

        label, _meta = self._message_sender_identity(sender)
        html_content = self._render_rich_markdown(text)
        html = self._message_card_html(
            sender_label=label,
            body_html=html_content,
            body_bg=c["bubble_assistant"],
            border_color=c["bubble_assistant_border"],
            label_color=acc,
            align_right=False,
        )
        self._append_html_block(html)

    _FENCE_RE = re.compile(
        r"```([A-Za-z0-9_+\-\.]*)[ \t]*\n([\s\S]*?)\n?```",
        re.MULTILINE,
    )

    def _render_rich_markdown(self, text: str) -> str:
        """Render CLI output as HTML with Pygments-highlighted code blocks.

        Fenced blocks (``` … ```) are pulled out *before* the markdown
        renderer runs, highlighted by Pygments with inline styles so they
        survive QTextEdit's limited CSS, and then stitched back in as raw
        HTML placeholders. The rest of the message is still handed to the
        markdown library for bold/italic/lists/tables/line-breaks.
        """
        placeholders: List[str] = []

        def _sub(match: "re.Match[str]") -> str:
            lang = (match.group(1) or "").strip().lower()
            code = match.group(2)
            highlighted = self._render_code_block(code, lang)
            placeholders.append(highlighted)
            return f"\x00CODE{len(placeholders) - 1}\x00"

        stripped = self._FENCE_RE.sub(_sub, text)
        try:
            html = markdown.markdown(
                stripped,
                extensions=["tables", "nl2br"],
            )
        except Exception:  # pragma: no cover - be forgiving on bad input
            html = escape(stripped).replace("\n", "<br>")

        # Re-insert highlighted code blocks.
        def _restore(m: "re.Match[str]") -> str:
            idx = int(m.group(1))
            return placeholders[idx] if 0 <= idx < len(placeholders) else m.group(0)

        return re.sub(r"\x00CODE(\d+)\x00", _restore, html)

    def _render_code_block(self, code: str, lang: str) -> str:
        pre_bg = "#1E1E2E"
        pre_fg = "#F8F8F2"
        header_label = lang or "code"
        if _PYG_AVAILABLE and code.strip():
            lexer = _cached_lexer_by_name(lang) if lang else None
            if lexer is None:
                try:
                    lexer = guess_lexer(code)
                    header_label = lang or (lexer.aliases[0] if lexer.aliases else "code")
                except Exception:
                    lexer = None
            if lexer is not None:
                try:
                    body = _pyg_highlight(code, lexer, _PYG_FORMATTER)
                except Exception:  # pragma: no cover - defensive
                    body = escape(code).replace("\n", "<br>")
            else:
                body = escape(code).replace("\n", "<br>")
        else:
            body = escape(code).replace("\n", "<br>")

        return (
            f"<div style='margin:6px 0;border-radius:8px;overflow:hidden;"
            f"border:1px solid #33384a;background:{pre_bg};'>"
            f"<div style='padding:4px 10px;font-size:10px;color:#8a8fa3;"
            f"background:#151720;letter-spacing:.5px;text-transform:uppercase;"
            f"font-family:Consolas,\"Courier New\",monospace;'>"
            f"{escape(header_label)}</div>"
            f"<pre style='margin:0;padding:10px 12px;color:{pre_fg};"
            f"font-family:Consolas,\"Courier New\",monospace;font-size:12px;"
            f"white-space:pre-wrap;word-wrap:break-word;'>{body}</pre>"
            f"</div>"
        )

    # ------------------------------------------------------------------ move

    def _place_near_anchor(self, anchor_rect: QRect, taskbar_edge: Optional[int]) -> QPoint:
        self.adjustSize()
        w = max(self.width(), 1)
        h = max(self.height(), 1)
        return point_for_window_near_anchor(anchor_rect, w, h, 14, taskbar_edge)

    def show_at(self, pos: QPoint, taskbar_edge: Optional[int] = None) -> None:
        anchor = QRect(pos.x() - 1, pos.y() - 1, 2, 2)
        self._anchor_rect = QRect(anchor)
        self._taskbar_edge = taskbar_edge
        self.move(self._place_near_anchor(anchor, taskbar_edge))
        self.show()
        self._was_visible = True
        self.raise_()
        self.input_field.setFocus()
        self._flush_pending_greeting()

    def show_for_rect(self, anchor_rect: QRect, taskbar_edge: Optional[int]) -> None:
        self._anchor_rect = QRect(anchor_rect)
        self._taskbar_edge = taskbar_edge
        self.move(self._place_near_anchor(anchor_rect, taskbar_edge))
        self.show()
        self._was_visible = True
        self.raise_()
        self.input_field.setFocus()
        self._flush_pending_greeting()

    def move_for_rect(self, anchor_rect: QRect, taskbar_edge: Optional[int]) -> None:
        self._anchor_rect = QRect(anchor_rect)
        self._taskbar_edge = taskbar_edge
        self.move(self._place_near_anchor(anchor_rect, taskbar_edge))

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event) -> None:
        was_visible = self._was_visible
        self._was_visible = False
        if self._busy_timer.isActive():
            self._busy_timer.stop()
        super().hideEvent(event)
        if was_visible:
            self.closed.emit()
