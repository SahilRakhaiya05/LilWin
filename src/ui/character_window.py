
import logging
import math
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from PyQt6.QtCore import QFileSystemWatcher, QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QBrush, QColor, QCursor, QGuiApplication, QPainter, QPen, QPixmap, QTransform
from PyQt6.QtWidgets import QLabel, QWidget

from services.character_registry import DEFAULT_WALK_TIMING_SPEC, Character, get_registry
from services.cli_sessions import create_cli_session
from services.openclaw_session import OpenClawSession
from services.provider_factory import is_provider_available, unavailable_message
from services.walker_prompts import (
    build_auto_collaboration_prompt,
    build_manual_channel_prompt,
)
from ui.terminal_popover import TerminalPopover
from ui.thinking_bubble import ThinkingBubble
from ui.walk_timing import WALK_BY_CHARACTER, WalkTiming, movement_position, random_walk_amount
from utils.config import ConfigManager

logger = logging.getLogger(__name__)
from utils.cursor_terminals import (
    count_integrated_provider_sessions_with_active_command,
    parse_terminal_messages,
    read_terminal_file,
)
from utils.terminal_sessions import (
    TerminalSessionInfo,
    list_all_terminal_sessions as list_terminal_sessions,
)
from utils.sound import SoundManager

if sys.platform == "win32":
    from utils.taskbar import ABE_BOTTOM, ABE_LEFT, ABE_RIGHT, ABE_TOP, get_taskbar_info
else:
    get_taskbar_info = None  # type: ignore
    ABE_LEFT = 0
    ABE_TOP = 1
    ABE_RIGHT = 2
    ABE_BOTTOM = 3


def _current_os_user_name() -> str:
    """Return a short, MrCat-friendly name for the current OS user.

    Preference order: $USER (unix-ish), $USERNAME (Windows), getpass.getuser(),
    then a bland fallback. We title-case the result so greetings read nicely
    (e.g. "admin" -> "Admin").
    """
    name = ""
    for key in ("USER", "USERNAME"):
        v = os.environ.get(key, "").strip()
        if v:
            name = v
            break
    if not name:
        try:
            import getpass
            name = getpass.getuser()
        except Exception:
            name = ""
    name = (name or "friend").strip()
    # Strip "DOMAIN\user" prefixes you sometimes see on Windows.
    if "\\" in name:
        name = name.rsplit("\\", 1)[-1]
    # Windows usernames are often all-lowercase; title-case looks friendlier.
    if name.islower():
        name = name.title()
    return name


def _resolve_greeting_for_user(greeting: Optional[str]) -> str:
    """Substitute `{user}` in a greeting template with the OS user name.

    If the template does not contain any placeholder we still try to insert
    the user name in a natural way so the old "Hey, Jazz" style greetings
    (where the character awkwardly addressed themselves) now read like the
    character greeting the user ("Hey, Admin.").
    """
    text = (greeting or "").strip()
    if not text:
        return ""
    user = _current_os_user_name()
    if "{user}" in text:
        return text.replace("{user}", user)
    # Heuristic rewrite of legacy greetings that hard-coded the character
    # name. We don't want to butcher the sentence, so only the most common
    # patterns from config/characters.json are handled.
    import re as _re
    # "Hey, Bruce here — what are we building?"
    #   -> "Hey, {user}. Bruce here — what are we building?"
    m = _re.match(r"^(Hey|Hi|Hello|Yo)[, ]+([A-Z][A-Za-z]+)\s+here\b", text)
    if m:
        greet = m.group(1)
        char_name = m.group(2)
        # Keep the "<Name> here …" part intact so the character still
        # identifies itself; just prepend a user-addressed greeting.
        rest = text[m.start(2):].strip()
        return f"{greet}, {user}. {rest}"
    # "Hey, Jazz. Rest..."     -> "Hey, {user}. Rest..."
    m = _re.match(r"^(Hey|Hi|Hello|Yo)[, ]+([A-Z][A-Za-z]+)[.!]\s*", text)
    if m:
        greet = m.group(1)
        rest = text[m.end():].strip()
        return f"{greet}, {user}. {rest}" if rest else f"{greet}, {user}."
    return text


def _expand_cli_path(raw_path: str, cwd: str) -> str:
    """Expand a user-entered path against the active CLI working directory."""
    text = (raw_path or "").strip().strip('"').strip("'")
    path = os.path.expanduser(os.path.expandvars(text))
    if not os.path.isabs(path):
        path = os.path.join(cwd, path)
    return os.path.normpath(os.path.abspath(path))


def _resolve_existing_dirish_path(raw_path: str, cwd: str) -> Tuple[Optional[str], List[str]]:
    """Resolve a directory path with best-effort auto-matching."""
    target = _expand_cli_path(raw_path, cwd)
    if os.path.isdir(target):
        return target, []

    parent = os.path.dirname(target) or cwd
    leaf = os.path.basename(target).strip()
    if not leaf or not os.path.isdir(parent):
        return None, []

    try:
        entries = sorted(os.listdir(parent))
    except OSError:
        return None, []

    dirs = [os.path.join(parent, name) for name in entries if os.path.isdir(os.path.join(parent, name))]
    leaf_low = leaf.lower()

    exact = [p for p in dirs if os.path.basename(p).lower() == leaf_low]
    if len(exact) == 1:
        return exact[0], []

    starts = [p for p in dirs if os.path.basename(p).lower().startswith(leaf_low)]
    if len(starts) == 1:
        return starts[0], []

    contains = [p for p in dirs if leaf_low in os.path.basename(p).lower()]
    if len(contains) == 1:
        return contains[0], []

    return None, (exact or starts or contains)[:8]


class CharacterWindow(QWidget):
    """Desktop walker: animation + taskbar motion aligned with macOS WalkerCharacter / LilAgentsController."""

    _completion_hints_ready = pyqtSignal(object)

    VIDEO_DURATION = 10.0
    SIZE_PRESETS = {"small": 100, "medium": 150, "large": 200}
    # Inset from each work-area side: inside this rect the character stays after drag; outside = snap to nearest edge.
    FREE_PLACEMENT_MARGIN_FRAC = 0.15

    def __init__(
        self,
        character_name: str = "bruce",
        *,
        provider_name: Optional[str] = None,
        walker_id: str = "",
        pinned_integrated_session: Optional[TerminalSessionInfo] = None,
        on_aux_closed: Optional[Callable[[], None]] = None,
    ) -> None:
        super().__init__()
        self.config = ConfigManager()
        self.settings = self.config.load()
        self._pinned_integrated_session = pinned_integrated_session
        self._integrated_session_walker = pinned_integrated_session is not None
        self._skip_config_writes = self._integrated_session_walker
        self._on_aux_closed = on_aux_closed
        self.walker_id = walker_id or ""
        self.character_name = character_name.lower()
        self._registry = get_registry()
        self._character_manually_pinned = False
        self._provider_follows_settings = provider_name is None
        self.provider_name = provider_name or self.settings.get("provider", "Claude")
        self._channel_id: Optional[str] = None
        self._auto_collab_enabled = False
        self._collab_role = "participant"
        self._collab_max_rounds = 4
        self._collab_partner_id: Optional[str] = None
        self.target_height = self.SIZE_PRESETS.get(self.settings.get("characterSize", "medium"), 150)
        self.assets_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
        self.frames_dir = os.path.join(self.assets_dir, f"{self.character_name}_frames")
        self._mem_walk_pixmaps: Optional[List[QPixmap]] = None
        self._disk_walk_paths: List[str] = []
        self._resolve_animation_frames()
        self.current_frame_idx = 0
        self._last_tick = time.monotonic()
        self._pixmap_cache: dict = {}
        self._alpha_inset_cache: Dict[int, Tuple[int, int, int, int]] = {}
        self._last_pixmap_key: Optional[Tuple[str, int, int, int]] = None
        self.session = None
        self._session_provider = None
        self._was_busy = False
        self._cli_cwd: Optional[str] = self.settings.get("cliWorkingDirectory") or None

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self.pos_progress = 0.0
        self.direction = 1
        self.is_paused = False

        self._is_walking_segment = False
        self._walk_start_time = 0.0
        self._walk_start_pos = 0.0
        self._walk_end_pos = 0.0
        self._walk_start_pixel = 0.0
        self._walk_end_pixel = 0.0
        self._going_right = True
        self._pause_end_time = time.monotonic() + random.uniform(0.5, 2.0)

        self.label = QLabel(self)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._char_drag_candidate = False
        self._char_dragging = False
        self._char_press_global = QPoint()
        self._char_drag_offset = QPoint()
        self._character_user_placed = False
        self._char_grabbed_mouse = False
        self._snap_edge: Optional[int] = None

        self._init_starting_position()
        self.update_frame()

        # Animation timer. We prefer 30 fps (33 ms) over 60 fps — the
        # sprite's 15-frame walk loop + linear movement looks identical on a
        # taskbar-sized sprite, and halving the tick rate noticeably drops
        # CPU on low-power laptops. When the walker is hidden we pause the
        # timer entirely (see showEvent/hideEvent).
        self._animation_interval_ms = 33
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.next_frame)
        self.timer.start(self._animation_interval_ms)

        self._external_cli_active = False
        self._last_external_provider_running = False
        self._external_monitor = QTimer(self)
        self._external_monitor.timeout.connect(self._poll_external_cli_activity)
        # Bumped from 2.5s -> 4s so the WMI / EnumWindows scan runs less
        # often (main CPU hog on Windows), still responsive enough for
        # "walker appears when you launch claude.exe".
        self._external_monitor.start(4000)

        self._show_cursor_terminal_link = bool(self.settings.get("showCursorTerminalLink", False))

        self.terminal = TerminalPopover(theme=self.settings.get("theme", "Peach"))
        self.terminal.set_session_linking_visible(self._show_cursor_terminal_link)
        self.terminal.reset_anchor_requested.connect(self._snap_terminal_near_character)
        self.thinking_bubble = ThinkingBubble()
        self.thinking_bubble.set_theme(self.settings.get("theme", "Peach"))
        self.sound_manager = SoundManager()
        self._apply_character_personality()
        self.terminal.message_sent.connect(self._handle_user_message)
        self.terminal.closed.connect(self._on_terminal_closed)
        self.terminal.clear_requested.connect(self._clear_session)
        self.terminal.session_selected.connect(self._select_session)
        self.terminal.restart_requested.connect(self._handle_restart_request)
        self.terminal.use_app_session_requested.connect(self._handle_use_app_session_request)
        self.terminal.set_context(self.provider_name, self.character_name)
        self._pending_completion = False
        self._completion_refresh_gen = 0
        self._linked_session_id = "app"
        self._linked_session_path: Optional[str] = None
        self._linked_session_signature = ""
        self._linked_session_kind: str = "app"   # "app" | "ide_file" | "process"
        self._linked_session_hwnd: int = 0
        self._linked_session_title: str = ""
        self._popover_states: Dict[str, Dict[str, Any]] = {}
        self._link_state_by_character: Dict[str, Tuple[str, str, str]] = {}
        self._snap_edge_by_character: Dict[str, Optional[int]] = {}
        self._last_session_error_text = ""
        self._last_session_error_at = 0.0
        # When False, chat stays near the tray/cursor until /resetpos or the window is closed.
        self._terminal_follows_character = True
        self._session_refresh = QTimer(self)
        self._session_refresh.timeout.connect(self._refresh_linkable_sessions)
        self._terminal_watcher = QFileSystemWatcher(self)
        self._terminal_watcher.fileChanged.connect(self._on_linked_terminal_file_changed)
        self._configure_session_refresh_interval()
        self._refresh_linkable_sessions()

        self._completion_hints_ready.connect(self.terminal.set_completion_strings)
        self._async_refresh_completions()

        self.update_position()
        if not self._defer_character_until_external_cli():
            self.show()

    def _async_refresh_completions(self) -> None:
        self._completion_refresh_gen += 1
        gen = self._completion_refresh_gen
        provider = self.provider_name

        def job() -> None:
            from utils.cli_input_hints import build_completion_list

            hints = build_completion_list(provider)
            if gen != self._completion_refresh_gen:
                return
            self._completion_hints_ready.emit(hints)

        threading.Thread(target=job, daemon=True).start()

    def present_on_desktop(self) -> None:
        """Show the walker on the desktop and bring it forward (it never renders inside an external console)."""
        self.show()
        state = self.windowState()
        if state & Qt.WindowState.WindowMinimized:
            self.setWindowState(
                (state & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive
            )
        self.raise_()
        self.activateWindow()

    def _repo_root(self) -> str:
        return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    def _stash_ui_for_character(self, char_key: str) -> None:
        char_key = char_key.lower()
        self._popover_states[char_key] = self.terminal.get_persisted_ui_state()
        self._link_state_by_character[char_key] = (
            self._linked_session_id,
            self._linked_session_path or "",
            self._linked_session_signature,
        )
        self._snap_edge_by_character[char_key] = self._snap_edge

    def _restore_ui_for_character(self, char_key: str) -> None:
        char_key = char_key.lower()
        st = self._popover_states.get(char_key)
        self.terminal.apply_persisted_ui_state(st)
        if not self._show_cursor_terminal_link:
            self._linked_session_id = "app"
            self._linked_session_path = None
            self._linked_session_signature = ""
        else:
            link = self._link_state_by_character.get(char_key, ("app", "", ""))
            self._linked_session_id = link[0]
            self._linked_session_path = link[1] if link[1] else None
            self._linked_session_signature = link[2]
        self._snap_edge = self._snap_edge_by_character.get(char_key)

    def _refresh_linkable_sessions(self) -> None:
        if self._integrated_session_walker:
            pin = self._pinned_integrated_session
            if not pin:
                self.terminal.set_session_options([("app", "In-app session")], "app")
                self._set_linked_terminal_watch(None)
                self.terminal.set_linked_input_mode(False)
                return
            if not self._show_cursor_terminal_link:
                self.terminal.set_link_sync_banner(None)
                self._set_linked_terminal_watch(None)
                self.terminal.set_session_options([("app", "In-app session")], "app")
                self.terminal.set_linked_input_mode(False)
                return
            opt_id = f"terminal:{pin.session_key}"
            self._linked_session_id = opt_id
            self._linked_session_path = pin.path or None
            self._linked_session_signature = ""
            self._linked_session_kind = getattr(pin, "kind", "ide_file")
            self._linked_session_hwnd = int(getattr(pin, "hwnd", 0) or 0)
            self._linked_session_title = pin.title
            self._set_linked_terminal_watch(pin.path if self._linked_session_kind == "ide_file" else None)
            self.terminal.set_input_enabled(True, self._bridge_input_placeholder())
            self.terminal.set_session_options([(opt_id, pin.title)], opt_id)
            if self._linked_session_kind == "ide_file":
                self._sync_linked_session_transcript()
            self._update_terminal_sync_banner(1)
            self.terminal.set_linked_input_mode(True)
            return

        if not self._show_cursor_terminal_link:
            self.terminal.set_link_sync_banner(None)
            self._set_linked_terminal_watch(None)
            if self._linked_session_id != "app":
                self._linked_session_id = "app"
                self._linked_session_path = None
                self._linked_session_signature = ""
                self._linked_session_kind = "app"
                self._linked_session_hwnd = 0
                self._linked_session_title = ""
                self._teardown_session()
                self.terminal.reset_chat()
                self.terminal.set_input_enabled(True)
                self.terminal.append_message("System", f"Using app-managed {self.provider_name} session.")
            self.terminal.set_session_options([("app", "In-app session")], "app")
            return

        options = [("app", "In-app session")]
        sessions = list_terminal_sessions(self._repo_root(), self.provider_name)
        current_exists = self._linked_session_id == "app"
        matched: Optional[TerminalSessionInfo] = None
        for session in sessions:
            option_id = f"terminal:{session.session_key}"
            options.append((option_id, session.title))
            if option_id == self._linked_session_id:
                current_exists = True
                matched = session

        # Auto-bind: if linking is on and we have sessions but nothing bound,
        # snap to the first one so the user doesn't need a dropdown picker.
        if not current_exists and sessions:
            first = sessions[0]
            self._linked_session_id = f"terminal:{first.session_key}"
            matched = first
            current_exists = True

        if not current_exists:
            self._linked_session_id = "app"
            self._linked_session_path = None
            self._linked_session_signature = ""
            self._linked_session_kind = "app"
            self._linked_session_hwnd = 0
            self._linked_session_title = ""
            self._set_linked_terminal_watch(None)
            self.terminal.set_input_enabled(True)
        else:
            if matched is not None:
                self._linked_session_path = matched.path or None
                self._linked_session_kind = matched.kind
                self._linked_session_hwnd = int(matched.hwnd or 0)
                self._linked_session_title = matched.title
            if self._linked_session_id != "app":
                # Only watch a disk path when the session actually has one (tier-1).
                self._set_linked_terminal_watch(self._linked_session_path if self._linked_session_kind == "ide_file" else None)
                self.terminal.set_input_enabled(True, self._bridge_input_placeholder())
            else:
                self._set_linked_terminal_watch(None)
                self.terminal.set_input_enabled(True)
        forced = self._pinned_integrated_session
        if forced and self._show_cursor_terminal_link:
            match = next((s for s in sessions if s.session_key == forced.session_key), None)
            if match:
                self._linked_session_id = f"terminal:{match.session_key}"
                self._linked_session_path = match.path or None
                self._linked_session_signature = ""
                self._linked_session_kind = match.kind
                self._linked_session_hwnd = int(match.hwnd or 0)
                self._linked_session_title = match.title
                self._set_linked_terminal_watch(match.path if match.kind == "ide_file" else None)
                self.terminal.set_input_enabled(True, self._bridge_input_placeholder())
            else:
                self._pinned_integrated_session = None
                self._linked_session_id = "app"
                self._linked_session_path = None
                self._linked_session_kind = "app"
                self._linked_session_hwnd = 0
                self._linked_session_title = ""
                self._set_linked_terminal_watch(None)
                self.terminal.set_input_enabled(True)
        self.terminal.set_session_options(options, self._linked_session_id)
        if self._linked_session_id != "app" and self._linked_session_kind == "ide_file":
            self._sync_linked_session_transcript()
        self._update_terminal_sync_banner(len(sessions))
        self.terminal.set_linked_input_mode(self._linked_session_id != "app")

    def _bridge_input_placeholder(self) -> str:
        if self.settings.get("linkBridgeAutoPaste", True) and sys.platform == "win32":
            return "Bridge: Enter → clipboard + auto Ctrl+V in IDE (click terminal first)"
        return "Bridge: Enter → clipboard — paste with Ctrl+V in the IDE terminal"

    def _update_terminal_sync_banner(self, sessions_count: int) -> None:
        if not self._show_cursor_terminal_link:
            self.terminal.set_link_sync_banner(None)
            return
        if self._linked_session_id != "app":
            auto = bool(self.settings.get("linkBridgeAutoPaste", True)) and sys.platform == "win32"
            enter_on = bool(self.settings.get("linkBridgeSendEnter", True))
            self.terminal.set_link_sync_banner(
                "Linked: transcript mirrors the IDE terminal file (live). "
                + (
                    "Enter copies your line, focuses the IDE, sends Ctrl+V"
                    + (" then Enter to submit" if enter_on and auto else "")
                    + " — click inside the terminal first if it misses."
                    if auto
                    else "Enter copies your line; paste with Ctrl+V in the IDE terminal."
                )
                + "  Type /session to switch to an in-app CLI instead."
            )
            return
        if sessions_count > 0:
            self.terminal.set_link_sync_banner(
                f"Two-way sync with your main CLI: pick a linked “{self.provider_name}” terminal above "
                f'(not “App session”). {sessions_count} matching session(s) found — that ties this window to the same session.'
            )
        else:
            self.terminal.set_link_sync_banner(
                f"No integrated-terminal capture yet for {self.provider_name}. "
                "Open the CLI in Cursor or VS Code’s terminal panel; this list fills from disk automatically."
            )

    def _select_session(self, session_id: str) -> None:
        if self._integrated_session_walker:
            return
        if session_id == self._linked_session_id:
            return
        if session_id == "app" and self._pinned_integrated_session is not None:
            self._pinned_integrated_session = None
        self._linked_session_id = session_id
        self._linked_session_signature = ""
        self._linked_session_path = None
        self._linked_session_kind = "app" if session_id == "app" else self._linked_session_kind
        self._linked_session_hwnd = 0 if session_id == "app" else self._linked_session_hwnd
        if session_id == "app":
            self._set_linked_terminal_watch(None)
            self.terminal.set_input_enabled(True)
            self.terminal.reset_chat()
            self.terminal.append_message("System", f"Using app-managed {self.provider_name} session.")
            self._refresh_linkable_sessions()
            return
        self._teardown_session()
        self.terminal.set_input_enabled(True, self._bridge_input_placeholder())
        self.terminal.reset_chat()
        self._refresh_linkable_sessions()
        self._sync_linked_session_transcript()

    def _configure_session_refresh_interval(self) -> None:
        self._session_refresh.stop()
        # Session discovery is only useful when terminal linking is enabled.
        # Keep it completely off in the default in-app mode so we don't scan
        # IDE terminals in the background for no reason.
        if not self._show_cursor_terminal_link and not self._integrated_session_walker:
            return
        # When linking is on, a slower poll still feels instant enough but
        # noticeably reduces CPU churn compared with the old sub-500 ms loop.
        ms = 1000 if self._show_cursor_terminal_link else 2500
        self._session_refresh.start(ms)

    def _set_linked_terminal_watch(self, path: Optional[str]) -> None:
        for p in list(self._terminal_watcher.files()):
            self._terminal_watcher.removePath(p)
        if path and os.path.isfile(path):
            self._terminal_watcher.addPath(path)

    def _on_linked_terminal_file_changed(self, path: str) -> None:
        if path and path == self._linked_session_path:
            self._terminal_watcher.removePath(path)
            if os.path.isfile(path):
                self._terminal_watcher.addPath(path)
        QTimer.singleShot(90, self._sync_linked_session_transcript)

    def _sync_linked_session_transcript(self) -> None:
        if self._linked_session_id == "app" or not self._linked_session_path:
            return
        _, body = read_terminal_file(self._linked_session_path)
        signature = str(hash(body))
        if signature == self._linked_session_signature:
            return
        self._linked_session_signature = signature
        messages = parse_terminal_messages(self.provider_name, body)
        if messages:
            self.terminal.set_messages(messages)
        else:
            self.terminal.set_messages(
                [("System", f"Linked to {self.provider_name} terminal session, but no chat text was parsed yet.")]
            )

    def _current_character(self) -> Optional[Character]:
        return self._registry.get(self.character_name)

    def _apply_character_personality(self) -> None:
        char = self._current_character()
        if char is None:
            return
        try:
            self.thinking_bubble.set_personality(
                list(char.personality.thinking_phrases),
                list(char.personality.done_phrases),
                char.personality.accent_color,
            )
        except AttributeError:
            pass
        try:
            self.sound_manager.set_variant(char.personality.sound_variant)
        except AttributeError:
            pass
        try:
            resolved = _resolve_greeting_for_user(char.personality.greeting)
            self.terminal.set_greeting(char.display_name, resolved)
        except AttributeError:
            pass

    def _walk_params(self) -> WalkTiming:
        roster = self._registry.names()
        fallback = roster[0] if roster else "bruce"
        wt = WALK_BY_CHARACTER.get(self.character_name, WALK_BY_CHARACTER.get(fallback))
        if wt is not None:
            return wt
        char = self._current_character()
        if char is not None:
            return WalkTiming.from_spec(char.walk_timing)
        return WalkTiming.from_spec(DEFAULT_WALK_TIMING_SPEC)

    def _get_float_setting(self, key: str, default: float) -> float:
        try:
            return float(self.settings.get(key, default))
        except (TypeError, ValueError):
            return default

    def _walk_duration(self) -> float:
        return max(4.0, self._get_float_setting("walkDurationSec", self.VIDEO_DURATION))

    def _external_monitor_enabled(self) -> bool:
        return bool(self.settings.get("monitorExternalCli", True))

    def _pause_range(self, short: bool) -> Tuple[float, float]:
        if short:
            lo = self._get_float_setting("shortPauseMinSec", 2.0)
            hi = self._get_float_setting("shortPauseMaxSec", 5.0)
        else:
            lo = self._get_float_setting("pauseMinSec", 5.0)
            hi = self._get_float_setting("pauseMaxSec", 12.0)
        lo = max(0.2, lo)
        hi = max(lo, hi)
        return lo, hi

    def _provider_process_matchers(self) -> List[str]:
        name = self.provider_name.lower()
        if name == "claude":
            return ["claude"]
        if name == "gemini":
            return ["gemini"]
        if name == "codex":
            return ["codex"]
        if name == "copilot":
            return ["copilot"]
        if name == "opencode":
            return ["opencode"]
        if name == "openclaw":
            return ["openclaw"]
        return [name]

    @staticmethod
    def _is_lil_agents_headless_cli(cmdline: str) -> bool:
        c = cmdline.lower()
        if "stream-json" in c and ("-p" in c or "--print" in c):
            return True
        return False

    @staticmethod
    def _cmdline_looks_like_ide_hosted_cli(cmdline: str) -> bool:
        """Heuristic: CLI looks spawned under Cursor / VS Code / JetBrains / VS, not a plain cmd session."""
        c = cmdline.lower()
        hints = (
            "cursor",
            "\\code.exe",
            "vscode",
            "pycharm",
            "jetbrains",
            "idea64",
            "rider64",
            "webstorm",
            "clion64",
            "devenv.exe",
            "azuredatastudio",
            "\\microsoft visual studio\\",
        )
        return any(h in c for h in hints)

    def _character_visibility_mode(self) -> str:
        v = str(self.settings.get("characterVisibilityMode", "")).lower()
        if v in ("always", "external_cli", "standalone_external_cli"):
            return v
        if self.settings.get("hideCharacterUntilExternalCli"):
            return "external_cli"
        return "always"

    def _defer_character_until_external_cli(self) -> bool:
        if self._character_visibility_mode() == "always":
            return False
        if self.provider_name == "OpenClaw":
            return False
        if sys.platform != "win32":
            return False
        return True

    def user_must_start_external_terminal_first(self) -> bool:
        if self._character_visibility_mode() == "always":
            return False
        return (
            sys.platform == "win32"
            and self.provider_name != "OpenClaw"
            and not bool(self._last_external_provider_running)
        )

    def set_pinned_integrated_session(self, info: Optional[TerminalSessionInfo]) -> None:
        """Primary window only: auto-link to one integrated terminal while multi-session walkers are active."""
        if self._integrated_session_walker:
            return
        if info is None:
            if self._pinned_integrated_session is None:
                return
            self._pinned_integrated_session = None
        else:
            old = self._pinned_integrated_session
            if old and old.session_key == info.session_key:
                self._pinned_integrated_session = info
                if self._linked_session_path != info.path:
                    self._linked_session_path = info.path
                    self._set_linked_terminal_watch(info.path)
                    self._sync_linked_session_transcript()
                return
            self._pinned_integrated_session = info
        self._refresh_linkable_sessions()
        self._poll_external_cli_activity()

    def _detect_external_provider_process(self, *, standalone_only: bool = False) -> bool:
        if sys.platform != "win32":
            return True
        # Fast path — unified tier-2 scanner. Finds node-hosted CLIs too and
        # avoids a 3-second PowerShell spawn every poll once the WMI cache is
        # warm. If it finds at least one matching session we're done.
        try:
            from utils.terminal_sessions import list_all_terminal_sessions
            quick = list_all_terminal_sessions(self._repo_root(), self.provider_name)
            if quick:
                if not standalone_only:
                    return True
                # "standalone only" mode: skip sessions that belong to an IDE
                # host (those come from tier-1 file scanning).
                for s in quick:
                    if getattr(s, "kind", "ide_file") == "process":
                        return True
        except Exception:
            logger.debug("fast tier-2 detection failed", exc_info=True)

        command = [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process | Select-Object Name,CommandLine | ConvertTo-Json -Compress",
        ]
        try:
            output = subprocess.check_output(
                command,
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            raw = json.loads(output) if output.strip() else []
        except Exception:
            return False

        processes = [raw] if isinstance(raw, dict) else raw
        needles = self._provider_process_matchers()
        for process in processes:
            name = str(process.get("Name") or "").lower()
            cmdline = str(process.get("CommandLine") or "").lower()
            combined = f"{name} {cmdline}"
            if any(needle in combined for needle in needles):
                if self._is_lil_agents_headless_cli(cmdline):
                    continue
                if standalone_only and self._cmdline_looks_like_ide_hosted_cli(cmdline):
                    continue
                return True
        return False

    def poll_external_cli_once(self) -> None:
        self._poll_external_cli_activity()

    def _poll_external_cli_activity(self) -> None:
        if self._integrated_session_walker:
            return
        # Perf: skip the WMI/EnumWindows scan when the user has already
        # hidden the character and isn't actively looking at the popover.
        # We keep polling if the visibility mode is "defer until external
        # CLI" (that's the whole reason the walker is hidden).
        if (
            not self.isVisible()
            and not self.terminal.isVisible()
            and not self._defer_character_until_external_cli()
        ):
            return
        need_scan = self._defer_character_until_external_cli() or self._external_monitor_enabled()
        standalone = self._character_visibility_mode() == "standalone_external_cli"
        external_running = False
        if need_scan:
            if sys.platform == "win32":
                external_running = self._detect_external_provider_process(
                    standalone_only=standalone
                )
            else:
                external_running = True

        if (
            not external_running
            and sys.platform == "win32"
            and not standalone
            and self.provider_name != "OpenClaw"
        ):
            if count_integrated_provider_sessions_with_active_command(
                self._repo_root(), self.provider_name
            ) > 0:
                external_running = True

        # If the walker-slot manager has already pinned us to a live session
        # (terminal link mode), count that as "external running" so we don't
        # hide the character between polls while the IDE terminal is active.
        if not external_running and self._pinned_integrated_session is not None:
            external_running = True

        self._last_external_provider_running = external_running

        if self._defer_character_until_external_cli():
            if external_running:
                if not self.isVisible():
                    self.show()
            else:
                self._external_cli_active = False
                self.thinking_bubble.hide_bubble()
                if self.terminal.isVisible():
                    self.terminal.hide()
                if self.isVisible():
                    self.hide()

        if not self._external_monitor_enabled():
            if not self._defer_character_until_external_cli() and self._external_cli_active:
                self._external_cli_active = False
                if not self._was_busy:
                    self.thinking_bubble.hide_bubble()
            return

        if self.terminal.isVisible() or self._was_busy:
            return

        # Mere "external CLI process exists" is NOT an 'AI is thinking' signal
        # — the bubble would pop up at startup or any time claude.exe/gemini.exe
        # is idle in the background, which is misleading. The bubble is driven
        # by real busy-state signals from the session (see :meth:`set_busy`)
        # and by turn completion. Here we just track the flag.
        if external_running and not self._external_cli_active:
            self._external_cli_active = True
        elif not external_running and self._external_cli_active:
            self._external_cli_active = False
            self.thinking_bubble.hide_bubble()

    def _scale_mac_px(self, mac_px: float) -> int:
        return int(mac_px * (self.target_height / 200.0))

    def _init_starting_position(self) -> None:
        if self.character_name == "jazz":
            self.pos_progress = 0.7
            self._going_right = random.choice([True, False])
        else:
            self.pos_progress = 0.3
            self._going_right = random.choice([True, False])
        self.direction = 1 if self._going_right else -1

    def _movement_at(self, video_time: float) -> float:
        w = self._walk_params()
        return movement_position(
            video_time,
            w.accel_start,
            w.full_speed_start,
            w.decel_start,
            w.walk_stop,
        )

    def _anchor_global_top_center(self) -> QPoint:
        g = self.frameGeometry()
        return QPoint(g.center().x(), g.top())

    def _anchor_rect(self) -> QRect:
        return self.frameGeometry()

    def _host_app(self):
        from PyQt6.QtWidgets import QApplication

        return QApplication.instance()

    def walker_display_name(self) -> str:
        char = self._current_character()
        return char.display_name if char else self.character_name.title()

    def set_channel_state(
        self,
        *,
        channel_id: Optional[str],
        auto_collab: bool,
        collab_role: str,
        max_rounds: int,
        collab_partner_id: Optional[str] = None,
    ) -> None:
        self._channel_id = (channel_id or "").strip().lower() or None
        self._auto_collab_enabled = bool(auto_collab)
        self._collab_role = (collab_role or "participant").strip() or "participant"
        self._collab_max_rounds = max(1, int(max_rounds))
        self._collab_partner_id = (collab_partner_id or "").strip() or None

    def last_assistant_text(self) -> str:
        return getattr(self.terminal, "_last_assistant_plain", "") or ""

    def _save_settings(self) -> None:
        if self._skip_config_writes:
            return
        self.settings["theme"] = self.terminal.theme
        size_name = min(self.SIZE_PRESETS, key=lambda k: abs(self.SIZE_PRESETS[k] - self.target_height))
        self.settings["characterSize"] = size_name
        self.config.save(self.settings)

    def _apply_size_preset(self) -> None:
        size_name = str(self.settings.get("characterSize", "medium")).lower()
        self.target_height = self.SIZE_PRESETS.get(size_name, 150)

    def set_initial_pause_range(self, low: float, high: float) -> None:
        self._pause_end_time = time.monotonic() + random.uniform(low, high)

    def _create_session(self):
        if self.provider_name == "OpenClaw":
            from utils.secrets import SecretsManager
            gateway = self.settings.get("gatewayURL", "ws://localhost:3001")
            token = SecretsManager().auth_token()
            return OpenClawSession(gateway, token)
        return create_cli_session(self.provider_name, cwd=self._cli_cwd)

    def set_cli_cwd(self, cwd: Optional[str]) -> str:
        """Switch the CLI session's working directory.

        Restarts the underlying session so the new CWD applies immediately.
        Returns the resolved path actually in use.
        """
        resolved: Optional[str] = None
        if cwd:
            path = os.path.abspath(os.path.expanduser(os.path.expandvars(cwd)))
            if os.path.isdir(path):
                resolved = path
        self._cli_cwd = resolved
        if not self._skip_config_writes:
            if resolved:
                self.settings["cliWorkingDirectory"] = resolved
            else:
                self.settings.pop("cliWorkingDirectory", None)
            self.config.save(self.settings)
        # Apply to current live session if any, then bounce it so the new
        # CWD takes effect on the next spawn.
        if self.session is not None and hasattr(self.session, "set_cwd"):
            try:
                self.session.set_cwd(resolved)
            except Exception:
                logger.debug("set_cwd failed on live session", exc_info=True)
        self._teardown_session()
        return resolved or os.path.expanduser("~")

    def get_cli_cwd(self) -> str:
        p = self._cli_cwd
        if p and os.path.isdir(p):
            return p
        return os.path.expanduser("~")

    def _ensure_session(self):
        if self.session is not None and self._session_provider == self.provider_name:
            return
        self._teardown_session()
        if not is_provider_available(self.provider_name):
            self.terminal.append_message("System", unavailable_message(self.provider_name))
            return
        self.session = self._create_session()
        self._session_provider = self.provider_name
        if self.session is None:
            self.terminal.append_message("System", f"Provider not implemented: {self.provider_name}")
            return
        self.session.text_received.connect(
            lambda text: self.terminal.append_message(self.provider_name, text.strip())
        )
        self.session.error_occurred.connect(self._append_session_error)
        self.session.tool_used.connect(lambda text: self.terminal.append_message("Tool", text.strip()))
        self.session.busy_state_changed.connect(self.set_busy)
        self.session.turn_completed.connect(self._on_turn_completed)
        self.session.start()

    def _teardown_session(self) -> None:
        if self.session is not None:
            self.session.terminate()
            self.session = None
            self._session_provider = None
        self._last_session_error_text = ""
        self._last_session_error_at = 0.0
        self.terminal.set_busy_state(False)
        self._was_busy = False
        self._external_cli_active = False
        self.thinking_bubble.hide_bubble()

    def _append_session_error(self, text: str) -> None:
        msg = (text or "").strip()
        if not msg:
            return
        now = time.monotonic()
        if msg == self._last_session_error_text and (now - self._last_session_error_at) < 2.5:
            return
        self._last_session_error_text = msg
        self._last_session_error_at = now
        self.terminal.append_message("Error", msg)

    def _clear_session(self) -> None:
        self._teardown_session()
        self.terminal.reset_chat()
        if self._linked_session_id != "app":
            self._linked_session_signature = ""
            self._sync_linked_session_transcript()
            self.terminal.append_message("System", "Cleared the popover; live mirror reloaded from the linked terminal.")
        else:
            self.terminal.append_message("System", f"{self.provider_name} session cleared.")

    def _handle_restart_request(self) -> None:
        """User ran ``/restart`` — bounce the CLI process (or, in linked mode,
        drop the link and spin up a fresh app-managed session)."""
        if self._linked_session_id != "app":
            # In linked mode the "session" is an external terminal, so the
            # most useful restart is: unlink and use a fresh app session.
            self._pinned_integrated_session = None
            self._select_session("app")
            self.terminal.append_message(
                "System",
                f"Dropped linked terminal — {self.provider_name} now runs in-app. "
                "Use /session again if you want to relink.",
            )
            return
        had_session = self.session is not None
        self._teardown_session()
        self.terminal.reset_chat()
        self._ensure_session()
        if self.session is not None:
            self.terminal.append_message(
                "System",
                f"{self.provider_name} session {'restarted' if had_session else 'started'}.",
            )

    def _handle_use_app_session_request(self) -> None:
        """User ran ``/session`` — force app-managed mode."""
        if self._linked_session_id == "app" and self._pinned_integrated_session is None:
            self.terminal.append_message(
                "System", f"Already using the app-managed {self.provider_name} session."
            )
            return
        self._pinned_integrated_session = None
        self._select_session("app")

    # ---------------------------------------------------------------- app cmds

    _APP_COMMAND_HINTS = (
        "/cd", "/pwd", "/ls", "/run",
        "/link", "/unlink", "/who",
        "/theme", "/size", "/spawn",
        "/provider", "/channel", "/tell", "/collab",
    )

    def _maybe_handle_app_command(self, text: str) -> bool:
        """Handle app-local "shell-like" commands.

        These turn the popover into a tiny terminal: the user can change the
        working directory of the CLI session, list files, run arbitrary
        commands, pick which external terminal to link to, swap themes, etc.
        Returns True when the input was consumed and should NOT be forwarded
        to the CLI / linked terminal.
        """
        stripped = text.strip()
        if not stripped.startswith("/"):
            return False

        parts = stripped.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/cd":
            self._cmd_cd(arg)
            return True
        if cmd == "/pwd":
            self.terminal.append_message("System", f"cwd: {self.get_cli_cwd()}")
            return True
        if cmd == "/ls":
            self._cmd_ls(arg)
            return True
        if cmd == "/run":
            self._cmd_run(arg)
            return True
        if cmd == "/link":
            self._cmd_link(arg)
            return True
        if cmd == "/unlink":
            self._cmd_unlink()
            return True
        if cmd == "/who":
            self._cmd_who()
            return True
        if cmd == "/theme":
            self._cmd_theme(arg)
            return True
        if cmd == "/size":
            self._cmd_size(arg)
            return True
        if cmd == "/spawn":
            self._cmd_spawn(arg)
            return True
        if cmd == "/provider":
            self._cmd_provider(arg)
            return True
        if cmd == "/channel":
            self._cmd_channel(arg)
            return True
        if cmd == "/tell":
            self._cmd_tell(arg)
            return True
        if cmd == "/collab":
            self._cmd_collab(arg)
            return True
        return False

    def _cmd_cd(self, arg: str) -> None:
        if not arg:
            self.terminal.append_message(
                "System",
                "usage: /cd <path>   (~, env vars, and relative paths are expanded)",
            )
            return
        entered = arg.strip().strip('"').strip("'")
        resolved, suggestions = _resolve_existing_dirish_path(entered, self.get_cli_cwd())
        if not resolved:
            if suggestions:
                body = ["No exact directory match. Did you mean:"]
                body.extend(f"  {p}" for p in suggestions)
                self.terminal.append_message("System", "\n".join(body))
            else:
                self.terminal.append_message("Error", f"Not a directory: {_expand_cli_path(entered, self.get_cli_cwd())}")
            return
        auto_matched = os.path.normcase(resolved) != os.path.normcase(_expand_cli_path(entered, self.get_cli_cwd()))
        resolved = self.set_cli_cwd(resolved)
        if os.path.isdir(resolved):
            self.terminal.append_message(
                "System",
                ((f"Auto-matched: {resolved}\n" if auto_matched else "") + f"cwd → {resolved}\nSession restarted; next message runs from here."),
            )

    def _cmd_ls(self, arg: str) -> None:
        root = arg.strip().strip('"').strip("'") or self.get_cli_cwd()
        auto_note = ""
        if arg.strip():
            path, suggestions = _resolve_existing_dirish_path(root, self.get_cli_cwd())
            if not path:
                if suggestions:
                    body = ["No exact directory match. Did you mean:"]
                    body.extend(f"  {p}" for p in suggestions)
                    self.terminal.append_message("System", "\n".join(body))
                else:
                    self.terminal.append_message("Error", f"Not a directory: {_expand_cli_path(root, self.get_cli_cwd())}")
                return
            if os.path.normcase(path) != os.path.normcase(_expand_cli_path(root, self.get_cli_cwd())):
                auto_note = f"(auto-matched from {root})"
        else:
            path = self.get_cli_cwd()
        try:
            entries = sorted(os.listdir(path))
        except OSError as exc:
            self.terminal.append_message("Error", f"ls failed: {exc}")
            return
        lines: List[str] = [f"{path} {auto_note}".rstrip()]
        for name in entries:
            full = os.path.join(path, name)
            tag = "/" if os.path.isdir(full) else ""
            lines.append(f"  {name}{tag}")
        self.terminal.append_message(self.provider_name, "```\n" + "\n".join(lines) + "\n```")

    def _cmd_run(self, arg: str) -> None:
        cmdline = arg.strip()
        if not cmdline:
            self.terminal.append_message("System", "usage: /run <command>   (runs in the current /pwd)")
            return

        cwd = self.get_cli_cwd()

        def _job() -> None:
            try:
                proc = subprocess.run(
                    cmdline,
                    shell=True,
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                out = (proc.stdout or "") + (proc.stderr or "")
                body = out.rstrip() or f"(exit {proc.returncode})"
                # Trim to keep the popover responsive on giant outputs.
                if len(body) > 4000:
                    body = body[:4000] + "\n… (truncated)"
                self.terminal.append_message(
                    self.provider_name,
                    f"`$ {cmdline}` (cwd=`{cwd}`)\n```\n{body}\n```",
                )
            except subprocess.TimeoutExpired:
                self.terminal.append_message("Error", f"/run timed out after 30s: {cmdline}")
            except Exception as exc:  # pragma: no cover - defensive
                self.terminal.append_message("Error", f"/run failed: {exc}")

        threading.Thread(target=_job, daemon=True).start()
        self.terminal.append_message("System", f"$ {cmdline}")

    def _cmd_link(self, arg: str) -> None:
        from utils.terminal_sessions import list_all_active_provider_sessions

        sessions = list_all_active_provider_sessions(self._repo_root(), self.provider_name)
        if not arg:
            if not sessions:
                self.terminal.append_message(
                    "System",
                    "No external terminal sessions running the "
                    f"{self.provider_name} CLI were found. Start it in a "
                    "terminal (Cursor / VS Code / Windows Terminal / …) and "
                    "type /link again.",
                )
                return
            lines = [f"Available {self.provider_name} sessions:"]
            for idx, s in enumerate(sessions, 1):
                lines.append(f"  {idx}. {s.title or s.session_key}")
            lines.append("")
            lines.append("Run `/link <N>` to attach.  `/unlink` returns to app-session.")
            self.terminal.append_message("System", "\n".join(lines))
            return
        try:
            idx = int(arg.strip()) - 1
        except ValueError:
            self.terminal.append_message("Error", f"/link expects a number: {arg!r}")
            return
        if idx < 0 or idx >= len(sessions):
            self.terminal.append_message("Error", f"/link index out of range (found {len(sessions)} session(s)).")
            return
        self.set_pinned_integrated_session(sessions[idx])
        self.terminal.append_message(
            "System",
            f"Linked to: {sessions[idx].title or sessions[idx].session_key}.\n"
            "Your messages are now bridged to that terminal. /unlink to detach.",
        )

    def _cmd_unlink(self) -> None:
        if self._linked_session_id == "app" and self._pinned_integrated_session is None:
            self.terminal.append_message("System", "Already on the app-managed session.")
            return
        self._pinned_integrated_session = None
        self._select_session("app")
        self.terminal.append_message(
            "System",
            f"Unlinked — {self.provider_name} now runs in the in-app session.",
        )

    def _cmd_who(self) -> None:
        info = [
            f"provider:  {self.provider_name}",
            f"character: {self.character_name}",
            f"theme:     {self.terminal.theme}",
            f"cwd:       {self.get_cli_cwd()}",
            f"session:   {'app-managed' if self._linked_session_id == 'app' else 'linked → ' + (self._linked_session_title or self._linked_session_id)}",
            f"busy:      {self._was_busy}",
        ]
        self.terminal.append_message(self.provider_name, "```\n" + "\n".join(info) + "\n```")

    def _cmd_theme(self, arg: str) -> None:
        name = arg.strip()
        if not name:
            self.terminal.append_message(
                "System",
                "usage: /theme <Peach|Mint|Midnight|Paper|Neon>",
            )
            return
        self.set_theme(name.title() if name.islower() else name)
        self.terminal.append_message("System", f"Theme → {self.terminal.theme}")

    def _cmd_size(self, arg: str) -> None:
        name = arg.strip().lower()
        if name not in self.SIZE_PRESETS:
            self.terminal.append_message(
                "System",
                f"usage: /size <{' | '.join(self.SIZE_PRESETS)}>  (current: "
                f"{self.settings.get('characterSize', 'medium')})",
            )
            return
        self.settings["characterSize"] = name
        self._apply_size_preset()
        self._resolve_animation_frames()
        self.update_frame()
        if not self._skip_config_writes:
            self.config.save(self.settings)
        self.terminal.append_message("System", f"Character size → {name}")

    def _cmd_spawn(self, arg: str) -> None:
        """Ask the app to spin up an additional walker for a different character."""
        parts = arg.strip().split()
        name = parts[0].lower() if parts else ""
        provider_name = parts[1] if len(parts) > 1 else ""
        if not name:
            roster = ", ".join(self._registry.names()) if self._registry else ""
            self.terminal.append_message(
                "System",
                f"usage: /spawn <character> [provider]   (available: {roster})",
            )
            return
        if not self._registry.get(name):
            self.terminal.append_message("Error", f"Unknown character: {name!r}")
            return
        if self._on_aux_closed is not None:
            self.terminal.append_message(
                "System",
                "This walker is already a spawned extra; use the primary walker's popover.",
            )
            return
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        parent_app = getattr(app, "_spawn_extra_walker", None)
        if not callable(parent_app):
            self.terminal.append_message(
                "System",
                "Spawn is only supported from the primary walker.",
            )
            return
        ok = parent_app(name, provider_name or None)
        if ok:
            detail = f"{name}"
            if provider_name:
                detail += f" · {provider_name}"
            self.terminal.append_message("System", f"Spawned walker ({detail}).")
        else:
            self.terminal.append_message("Error", f"Could not spawn extra walker: {name}")

    def _cmd_provider(self, arg: str) -> None:
        raw = arg.strip()
        from services.provider_factory import PROVIDER_SPECS

        providers = ", ".join(PROVIDER_SPECS.keys())
        if not raw:
            self.terminal.append_message(
                "System",
                f"Current walker provider: {self.provider_name}\n"
                f"Available: {providers}\n"
                "Use `/provider <name>` to switch just this walker.",
            )
            return
        target = next((name for name in PROVIDER_SPECS if name.lower() == raw.lower()), None)
        if not target:
            self.terminal.append_message("Error", f"Unknown provider: {raw!r}")
            return
        app = self._host_app()
        setter = getattr(app, "set_walker_provider", None)
        if callable(setter) and self.walker_id:
            setter(self.walker_id, target)
        else:
            self.set_provider(target)

    def _cmd_channel(self, arg: str) -> None:
        raw = arg.strip()
        app = self._host_app()
        join = getattr(app, "join_walker_channel", None)
        leave = getattr(app, "leave_walker_channel", None)
        members_fn = getattr(app, "channel_members_for_walker", None)
        state_fn = getattr(app, "channel_state_for_walker", None)
        if not raw or raw == "status":
            state = state_fn(self.walker_id) if callable(state_fn) else None
            channel_id = getattr(state, "channel_id", None)
            if not channel_id:
                self.terminal.append_message(
                    "System",
                    "No channel joined. Use `/channel create <name>` or `/channel join <name>`.",
                )
                return
            members = members_fn(self.walker_id) if callable(members_fn) else []
            body = [f"channel: {channel_id}"]
            if members:
                body.append("members:")
                body.extend(f"  {m}" for m in members)
            self.terminal.append_message("System", "\n".join(body))
            return

        parts = raw.split(None, 1)
        cmd = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        if cmd in ("create", "join"):
            if not rest.strip():
                self.terminal.append_message("System", "usage: /channel create <name>")
                return
            if callable(join):
                joined = join(self.walker_id, rest.strip())
                self.terminal.append_message("System", f"Joined channel: {joined}")
            return
        if cmd == "leave":
            if callable(leave):
                leave(self.walker_id)
            self.terminal.append_message("System", "Left the current channel.")
            return
        if cmd == "members":
            members = members_fn(self.walker_id) if callable(members_fn) else []
            if not members:
                self.terminal.append_message("System", "No channel members found.")
                return
            self.terminal.append_message("System", "Channel members:\n" + "\n".join(f"  {m}" for m in members))
            return
        self.terminal.append_message(
            "System",
            "usage: /channel <status|create <name>|join <name>|leave|members>",
        )

    def _cmd_tell(self, arg: str) -> None:
        parts = arg.strip().split(None, 1)
        if len(parts) < 2:
            self.terminal.append_message(
                "System",
                "usage: /tell <character> <message>   "
                "(sends only to that walker's provider; normal typing stays local).",
            )
            return
        target_name, msg = parts[0], parts[1]
        app = self._host_app()
        relay = getattr(app, "relay_walker_channel_message", None)
        if not callable(relay):
            self.terminal.append_message("System", "/tell is not available here.")
            return
        ok, err = relay(self.walker_id, target_name, msg)
        if not ok:
            self.terminal.append_message("Error", err)
            return
        prov = ""
        to_disp = target_name
        res = getattr(app, "resolve_walker_id_by_character_label", None)
        wb = getattr(app, "_walker_by_id", None)
        tid = res(target_name) if callable(res) else None
        tw = wb(tid) if tid and callable(wb) else None
        if tw is not None:
            prov = getattr(tw, "provider_name", "") or ""
            to_disp = tw.walker_display_name()
        self.terminal.append_channel_delegation(
            from_character=self.walker_display_name(),
            to_character=to_disp,
            body=msg.strip(),
        )
        prov_bit = f" · {prov}" if prov else ""
        self.terminal.append_message(
            "System",
            f"✓ Delivered to {to_disp}{prov_bit} — this tab’s provider was not used.",
        )

    def _cmd_collab(self, arg: str) -> None:
        raw = arg.strip()
        app = self._host_app()
        configure = getattr(app, "configure_walker_collaboration", None)
        bind = getattr(app, "bind_walker_collab_partner", None)
        clear_partner = getattr(app, "clear_walker_collab_partner", None)
        partner_display = getattr(app, "walker_collab_partner_display", None)
        resolve = getattr(app, "resolve_walker_id_by_character_label", None)
        state_fn = getattr(app, "channel_state_for_walker", None)
        if not raw:
            state = state_fn(self.walker_id) if callable(state_fn) else None
            if state is None:
                self.terminal.append_message("System", "No collaboration state available.")
                return
            pair_line = ""
            if callable(partner_display):
                p = partner_display(self.walker_id)
                if p:
                    pair_line = f"\ncollab partner: {p}"
            self.terminal.append_message(
                "System",
                f"channel: {getattr(state, 'channel_id', None) or '(none)'}\n"
                f"auto-collab: {getattr(state, 'auto_collab', False)}\n"
                f"role: {getattr(state, 'collab_role', 'participant')}\n"
                f"max rounds: {getattr(state, 'max_rounds', 4)}"
                + pair_line,
            )
            return
        parts = raw.split()
        action = parts[0].lower()
        if action == "off":
            if callable(configure):
                configure(self.walker_id, enabled=False)
            self.terminal.append_message("System", "Auto-collaboration disabled (partner link cleared).")
            return
        if action == "clear":
            if callable(clear_partner):
                clear_partner(self.walker_id)
            self.terminal.append_message("System", "Collaboration partner cleared (auto-collab stays as-is).")
            return
        if action == "on":
            role = "participant"
            max_rounds = 4
            extras = parts[1:]
            partner_token: Optional[str] = None
            if extras:
                if extras[-1].isdigit():
                    max_rounds = max(1, int(extras[-1]))
                    extras = extras[:-1]
            if extras:
                cand = extras[0]
                resolved = resolve(cand) if callable(resolve) else None
                if resolved and resolved != self.walker_id:
                    partner_token = cand
                    extras = extras[1:]
                if extras:
                    role = " ".join(extras).strip() or "participant"
            if callable(configure):
                configure(self.walker_id, enabled=True, role=role, max_rounds=max_rounds)
            msg = f"Auto-collaboration on (role={role}, max_rounds={max_rounds})."
            if partner_token and callable(bind):
                _, err = bind(self.walker_id, partner_token)
                if err:
                    self.terminal.append_message("System", msg + f" Partner not linked: {err}")
                else:
                    label = partner_display(self.walker_id) if callable(partner_display) else partner_token
                    self.terminal.append_message(
                        "System",
                        msg + f" Paired with {label or partner_token} — turns hand off to their provider and back.",
                    )
            else:
                self.terminal.append_message("System", msg)
            return
        # Shorthand: /collab <character name> — pair with that walker and enable auto-collab
        partner_label = raw
        if not callable(bind) or not callable(configure):
            self.terminal.append_message("System", "Collaboration commands require the full app (not available).")
            return
        _, err = bind(self.walker_id, partner_label)
        if err:
            self.terminal.append_message("Error", err)
            return
        configure(self.walker_id, enabled=True, role="participant", max_rounds=4)
        label = partner_display(self.walker_id) if callable(partner_display) else partner_label
        self.terminal.append_message(
            "System",
            f"Paired with {label or partner_label}. Auto-collab on — after each reply, the other walker’s provider continues the thread.",
        )

    def _handle_user_message(self, text: str) -> None:
        if self._maybe_handle_app_command(text):
            return
        if self._linked_session_id != "app":
            clip = QGuiApplication.clipboard()
            clip.setText(text)
            self.terminal.append_message("User", text)
            auto_paste = bool(self.settings.get("linkBridgeAutoPaste", True)) and sys.platform == "win32"
            send_enter_after = bool(self.settings.get("linkBridgeSendEnter", True))
            kind = self._linked_session_kind
            target_hwnd = self._linked_session_hwnd
            if auto_paste:
                try:
                    from utils.win_paste_bridge import (
                        find_ide_host_window,
                        send_ctrl_v,
                        send_enter,
                        send_paste_to_hwnd,
                        try_set_foreground_window,
                    )

                    # Process-backed tier-2 sessions: target the session's exact HWND.
                    if kind == "process" and target_hwnd:
                        def _paste_proc() -> None:
                            send_paste_to_hwnd(target_hwnd, press_enter=send_enter_after)

                        QTimer.singleShot(90, _paste_proc)
                        self.terminal.append_message(
                            "System",
                            "↗ Sent to linked terminal (focus + paste"
                            + (" + Enter" if send_enter_after else "")
                            + ").",
                        )
                    else:
                        # Tier-1 IDE file sessions: fall back to title search.
                        hwnd = find_ide_host_window()
                        if hwnd:
                            try_set_foreground_window(hwnd)

                            def _paste() -> None:
                                send_ctrl_v()
                                if send_enter_after:
                                    QTimer.singleShot(115, send_enter)

                            QTimer.singleShot(130, _paste)
                            self.terminal.append_message(
                                "System",
                                "↗ Copied — focusing IDE, paste, then Enter"
                                + (" (submit)" if send_enter_after else "")
                                + "; click the terminal pane first if it misses.",
                            )
                        else:
                            self.terminal.append_message(
                                "System",
                                "↗ Copied — no matching terminal window found; paste manually (Ctrl+V).",
                            )
                except Exception:
                    logger.debug("paste bridge failed", exc_info=True)
                    self.terminal.append_message(
                        "System",
                        "↗ Copied — paste manually (Ctrl+V) in the linked terminal.",
                    )
            else:
                self.terminal.append_message(
                    "System",
                    "↗ Copied — paste with Ctrl+V in the linked terminal, or enable “Auto-paste bridge” in Settings.",
                )
            return
        app = self._host_app()
        routed = getattr(app, "handle_walker_user_message", None)
        if callable(routed) and self.walker_id:
            routed(self.walker_id, text)
        self._ensure_session()
        if self.session is None:
            return
        self.session.send(text)

    def receive_channel_prompt(
        self,
        *,
        channel_id: str,
        source_label: str,
        source_provider: str,
        prompt_text: str,
        auto_collab: bool = False,
        collab_goal: str = "",
        round_index: int = 0,
        max_rounds: int = 0,
        source_cwd: str = "",
    ) -> bool:
        if self._linked_session_id != "app":
            self.terminal.append_message(
                "System",
                f"Skipped channel prompt for {self.walker_display_name()} because this walker is linked to an external terminal.",
            )
            return False
        self._ensure_session()
        if self.session is None:
            return False
        target_cwd = self.get_cli_cwd()
        self.terminal.append_channel_delegation(
            from_character=source_label,
            to_character=self.walker_display_name(),
            body=prompt_text,
        )
        if auto_collab:
            wrapped = build_auto_collaboration_prompt(
                channel_id=channel_id,
                goal=collab_goal,
                source_label=source_label,
                source_provider=source_provider,
                target_label=self.walker_display_name(),
                target_provider=self.provider_name,
                target_role=self._collab_role,
                prior_response=prompt_text,
                round_index=round_index,
                max_rounds=max_rounds,
                source_cwd=source_cwd,
                target_cwd=target_cwd,
            )
            self.terminal.append_message(
                "System",
                f"⟳ #{channel_id} · auto-collab handoff → {self.provider_name}",
            )
        else:
            wrapped = build_manual_channel_prompt(
                channel_id=channel_id,
                source_label=source_label,
                source_provider=source_provider,
                target_label=self.walker_display_name(),
                target_provider=self.provider_name,
                message=prompt_text,
                source_cwd=source_cwd,
                target_cwd=target_cwd,
            )
            self.terminal.append_message(
                "System",
                f"⟳ #{channel_id} · delegated task → {self.provider_name}",
            )
        self.session.send(wrapped)
        return True

    def append_channel_response(
        self,
        channel_id: str,
        source_label: str,
        text: str,
        *,
        source_cwd: str = "",
    ) -> None:
        cwd = (source_cwd or "").strip()
        if cwd:
            self.terminal.append_message(
                "System",
                f"[channel {channel_id}] {source_label} cwd: {cwd}",
            )
        self.terminal.append_message(source_label, text)

    def _maybe_apply_provider_character(self, provider_name: str) -> None:
        if self._character_manually_pinned or self._integrated_session_walker:
            return
        mapping = self.settings.get("providerCharacterMap") or {}
        mapped = str(mapping.get(provider_name) or "").strip().lower()
        if not mapped:
            char = self._registry.default_for_provider(provider_name)
            mapped = char.name if char else ""
        if mapped and mapped != self.character_name and self._registry.get(mapped):
            self.set_character(mapped, manual=False)

    def set_provider(self, provider_name: str) -> None:
        if provider_name == self.provider_name:
            return
        self._provider_follows_settings = False
        self.provider_name = provider_name
        self._maybe_apply_provider_character(provider_name)
        if self._integrated_session_walker:
            self._popover_states.clear()
            self._link_state_by_character.clear()
            self._snap_edge_by_character.clear()
            self._snap_edge = None
            self.terminal.set_context(self.provider_name, self.character_name)
            self._teardown_session()
            self.terminal.reset_chat()
            self._refresh_linkable_sessions()
            self.terminal.append_message("System", f"Switched provider to {provider_name}.")
            self._async_refresh_completions()
            return
        self._pinned_integrated_session = None
        self._popover_states.clear()
        self._link_state_by_character.clear()
        self._snap_edge_by_character.clear()
        self._snap_edge = None
        self._linked_session_id = "app"
        self._linked_session_path = None
        self._linked_session_signature = ""
        self._linked_session_kind = "app"
        self._linked_session_hwnd = 0
        self._linked_session_title = ""
        self.terminal.set_context(self.provider_name, self.character_name)
        self._teardown_session()
        self.terminal.reset_chat()
        self._refresh_linkable_sessions()
        self.terminal.append_message("System", f"Switched provider to {provider_name}.")
        self._async_refresh_completions()
        self._poll_external_cli_activity()

    def set_theme(self, theme_name: str) -> None:
        self.terminal.apply_theme(theme_name)
        self.thinking_bubble.set_theme(theme_name)
        self._save_settings()

    def apply_saved_settings(self, data: dict) -> None:
        if self._integrated_session_walker:
            old_provider = self.provider_name
            old_settings = dict(self.settings)
            self.settings = dict(data)
            if self._provider_follows_settings:
                self.provider_name = self.settings.get("provider", self.provider_name)
            need_new_session = self.provider_name != old_provider
            if self.provider_name == "OpenClaw":
                if self.settings.get("gatewayURL") != old_settings.get("gatewayURL"):
                    need_new_session = True
                # authToken lives in the secrets store; SettingsDialog forces
                # a teardown when it changes, so no comparison is needed here.
                pass
            self._show_cursor_terminal_link = bool(self.settings.get("showCursorTerminalLink", False))
            self.terminal.set_session_linking_visible(self._show_cursor_terminal_link)
            self._configure_session_refresh_interval()
            self._apply_size_preset()
            self.terminal.apply_theme(self.settings.get("theme", self.terminal.theme))
            self.thinking_bubble.set_theme(self.settings.get("theme", "Peach"))
            self.terminal.set_context(self.provider_name, self.character_name)
            self._pixmap_cache.clear()
            self._last_pixmap_key = None
            self._external_cli_active = False
            if need_new_session:
                self._teardown_session()
            self._refresh_linkable_sessions()
            self._async_refresh_completions()
            if not self._defer_character_until_external_cli() and not self.isVisible():
                self.show()
            self.update_frame()
            self.update_position()
            return

        old_provider = self.provider_name
        old_settings = dict(self.settings)
        self.settings = dict(data)
        if self._provider_follows_settings:
            self.provider_name = self.settings.get("provider", self.provider_name)
        need_new_session = self.provider_name != old_provider
        if self.provider_name == "OpenClaw":
            if self.settings.get("gatewayURL") != old_settings.get("gatewayURL"):
                need_new_session = True
            # authToken lives in the secrets store; SettingsDialog forces
            # a teardown when it changes, so no comparison is needed here.
        self._show_cursor_terminal_link = bool(self.settings.get("showCursorTerminalLink", False))
        self.terminal.set_session_linking_visible(self._show_cursor_terminal_link)
        if not self._show_cursor_terminal_link:
            self._pinned_integrated_session = None
        self._configure_session_refresh_interval()
        self._popover_states.clear()
        self._link_state_by_character.clear()
        self._snap_edge_by_character.clear()
        self._snap_edge = None
        self._restore_ui_for_character(self.character_name)
        self._apply_size_preset()
        self.terminal.apply_theme(self.settings.get("theme", self.terminal.theme))
        self.thinking_bubble.set_theme(self.settings.get("theme", "Peach"))
        self.terminal.set_context(self.provider_name, self.character_name)
        self._pixmap_cache.clear()
        self._last_pixmap_key = None
        self._external_cli_active = False
        if need_new_session:
            self._teardown_session()
        self._refresh_linkable_sessions()
        self._async_refresh_completions()
        self._poll_external_cli_activity()
        if not self._defer_character_until_external_cli() and not self.isVisible():
            self.show()
        self.update_frame()
        self.update_position()

    def set_character(self, character_name: str, *, manual: bool = True) -> None:
        character_name = character_name.lower()
        if not self._registry.get(character_name):
            logger.warning("Unknown character '%s'; ignoring", character_name)
            return
        if manual:
            self._character_manually_pinned = True
        if character_name == self.character_name:
            return
        self._stash_ui_for_character(self.character_name)
        self.character_name = character_name
        char = self._registry.get(character_name)
        self.frames_dir = os.path.join(
            self.assets_dir, char.frames_dir if char else f"{character_name}_frames"
        )
        self._mem_walk_pixmaps = None
        self._disk_walk_paths = []
        self._resolve_animation_frames()
        self.current_frame_idx = 0
        self._pixmap_cache.clear()
        self._last_pixmap_key = None
        self._init_starting_position()
        self._is_walking_segment = False
        self._walk_start_pos = 0.0
        self._walk_end_pos = 0.0
        self._pause_end_time = time.monotonic() + random.uniform(0.5, 2.0)
        self._restore_ui_for_character(self.character_name)
        self.terminal.set_context(self.provider_name, self.character_name)
        self._apply_character_personality()
        self._refresh_linkable_sessions()
        self.update_frame()
        self.update_position()
        self._save_settings()

    def _resolve_animation_frames(self) -> None:
        self._extract_frames_from_video_if_possible()
        self._mem_walk_pixmaps = None
        self._disk_walk_paths = []
        paths = self._load_disk_frame_paths()
        if len(paths) > 1:
            if len(paths) < 12:
                self._mem_walk_pixmaps = self._build_stride_frames_from_png(paths[0])
                self._disk_walk_paths = []
            else:
                self._disk_walk_paths = paths
            return
        if len(paths) == 1:
            self._mem_walk_pixmaps = self._build_stride_frames_from_png(paths[0])
            return
        self._mem_walk_pixmaps = self._build_placeholder_pixmaps()

    def _walk_frame_count(self) -> int:
        if self._mem_walk_pixmaps:
            return len(self._mem_walk_pixmaps)
        return len(self._disk_walk_paths)

    def _find_walk_mov(self) -> Optional[str]:
        name = self.character_name
        candidates = [
            os.path.join(self.assets_dir, f"walk-{name}-01.mov"),
            os.path.join(
                self._repo_root(),
                "lil-agents-mac",
                "lil-agents-main",
                "LilAgents",
                f"walk-{name}-01.mov",
            ),
        ]
        for p in candidates:
            if os.path.isfile(p):
                return p
        return None

    def _find_ffmpeg(self) -> Optional[str]:
        direct = shutil.which("ffmpeg")
        if direct:
            return direct

        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            winget_root = os.path.join(local, "Microsoft", "WinGet", "Packages")
            if os.path.isdir(winget_root):
                for entry in os.listdir(winget_root):
                    if not entry.lower().startswith("gyan.ffmpeg"):
                        continue
                    candidate = os.path.join(
                        winget_root,
                        entry,
                        "ffmpeg-8.1-essentials_build",
                        "bin",
                        "ffmpeg.exe",
                    )
                    if os.path.isfile(candidate):
                        return candidate
        return None

    def _extract_frames_from_video_if_possible(self) -> None:
        min_frames = 24
        existing: List[str] = []
        if os.path.isdir(self.frames_dir):
            existing = sorted(
                [
                    os.path.join(self.frames_dir, f)
                    for f in os.listdir(self.frames_dir)
                    if f.endswith(".png") and f.lower().startswith("frame")
                ]
            )
            if len(existing) >= min_frames:
                return
            for p in existing:
                try:
                    os.remove(p)
                except OSError:
                    pass

        ffmpeg_path = self._find_ffmpeg()
        if not ffmpeg_path:
            return

        video_path = self._find_walk_mov()
        if not video_path:
            return

        os.makedirs(self.frames_dir, exist_ok=True)
        output_pattern = os.path.join(self.frames_dir, "frame%04d.png")
        try:
            subprocess.run(
                [
                    ffmpeg_path,
                    "-y",
                    "-i",
                    video_path,
                    "-vf",
                    "fps=30",
                    output_pattern,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
        except Exception:
            return

    def _load_disk_frame_paths(self) -> List[str]:
        if os.path.isdir(self.frames_dir):
            frames = sorted(
                [
                    os.path.join(self.frames_dir, f)
                    for f in os.listdir(self.frames_dir)
                    if f.endswith(".png") and f.lower().startswith("frame")
                ]
            )
            if frames:
                return frames
        fallback_candidates = [
            os.path.join(self.assets_dir, f"{self.character_name}-hang.png"),
            os.path.join(self.assets_dir, f"walk-{self.character_name}-01.mov.png"),
            os.path.join(self.assets_dir, f"walk-{self.character_name}-01.mov.webp"),
            os.path.join(self.assets_dir, "bruce-hang.png"),
            os.path.join(self.assets_dir, "walk-bruce-01.mov.png"),
            os.path.join(self.assets_dir, "walk-bruce-01.mov.webp"),
        ]
        for candidate in fallback_candidates:
            if os.path.isfile(candidate):
                return [candidate]
        return []

    def _build_stride_frames_from_png(self, path: str) -> List[QPixmap]:
        src = QPixmap(path)
        if src.isNull():
            return self._build_placeholder_pixmaps()
        base = src.scaledToHeight(self.target_height, Qt.TransformationMode.SmoothTransformation)
        w, h = base.width(), base.height()
        margin = 20
        frames: List[QPixmap] = []
        for i in range(90):
            t = (i / 90.0) * 2.0 * math.pi
            dx = int(6.0 * math.sin(t))
            dy = int(4.0 * math.cos(2.0 * t))
            pm = QPixmap(w + margin * 2, h + margin * 2)
            pm.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pm)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.drawPixmap(margin + dx, margin + dy, base)
            painter.end()
            frames.append(pm)
        return frames

    def _build_placeholder_pixmaps(self) -> List[QPixmap]:
        """~30 fps over 10s = 300 samples; use 100 frames mapped across VIDEO_DURATION (Mac-style leg cycle)."""
        frames: List[QPixmap] = []
        char = self._current_character()
        base_name = char.display_name if char else self.character_name.capitalize()
        accent = QColor(char.personality.accent_color) if char else QColor(255, 140, 105)
        if not accent.isValid():
            accent = QColor(255, 140, 105)
        palette = (accent.red(), accent.green(), accent.blue())
        body = QColor(*palette)
        shirt = QColor(
            min(255, palette[0] + 35),
            min(255, palette[1] + 35),
            min(255, palette[2] + 40),
        )
        foot = QColor(max(0, palette[0] - 55), max(0, palette[1] - 55), max(0, palette[2] - 55))
        outline = QColor(40, 40, 40)
        w, h = 160, int(self.target_height * 1.35)
        n = 100
        for i in range(n):
            t = (i / float(n)) * 2.0 * math.pi
            pm = QPixmap(w, h)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            sway = int(5.0 * math.sin(t))
            bounce = int(3.5 * abs(math.sin(t * 2.0)))
            cx = w // 2 + sway
            cy = h // 2 - 4 + bounce

            p.setPen(QPen(outline, 2))
            p.setBrush(QBrush(body))
            p.drawEllipse(cx - 34, cy - 52, 68, 62)

            p.setPen(QPen(outline, 2))
            p.setBrush(QBrush(QColor(255, 200, 80)))
            p.drawEllipse(cx - 3, cy - 62, 6, 18)

            arm_angle = 28 * math.sin(t)
            p.save()
            p.translate(cx - 28, cy - 18)
            p.rotate(-arm_angle)
            p.setPen(QPen(outline, 2))
            p.setBrush(QBrush(shirt))
            p.drawRoundedRect(0, 0, 10, 36, 4, 4)
            p.restore()
            p.save()
            p.translate(cx + 18, cy - 18)
            p.rotate(arm_angle)
            p.setPen(QPen(outline, 2))
            p.setBrush(QBrush(shirt))
            p.drawRoundedRect(-10, 0, 10, 36, 4, 4)
            p.restore()

            p.setPen(QPen(outline, 2))
            p.setBrush(QBrush(shirt))
            p.drawRoundedRect(cx - 30, cy - 8, 60, 44, 14, 14)

            leg_swing = int(12 * math.sin(t))
            p.setBrush(QBrush(foot))
            p.setPen(QPen(outline, 2))
            p.drawEllipse(cx - 20 + leg_swing, cy + 34, 18, 12)
            p.drawEllipse(cx + 2 - leg_swing, cy + 34, 18, 12)

            p.setBrush(QBrush(QColor(30, 30, 30)))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(cx - 18, cy - 38, 11, 12)
            p.drawEllipse(cx + 5, cy - 38, 11, 12)

            p.setPen(QPen(outline, 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawArc(cx - 16, cy - 22, 32, 18, 200 * 16, 140 * 16)

            p.setPen(QPen(outline, 2))
            p.drawText(8, h - 8, base_name)
            p.end()
            frames.append(pm)
        return frames

    def _taskbar_edge(self) -> Optional[int]:
        if sys.platform != "win32" or not get_taskbar_info:
            return None
        info = get_taskbar_info()
        return int(info["edge"]) if info else None

    def _free_placement_inner_rect(self, geom: QRect) -> QRect:
        mx = max(8, int(geom.width() * self.FREE_PLACEMENT_MARGIN_FRAC))
        my = max(8, int(geom.height() * self.FREE_PLACEMENT_MARGIN_FRAC))
        iw = max(1, geom.width() - 2 * mx)
        ih = max(1, geom.height() - 2 * my)
        return QRect(geom.left() + mx, geom.top() + my, iw, ih)

    def _character_center_in_free_placement_zone(self) -> bool:
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return False
        geom = screen.availableGeometry()
        inner = self._free_placement_inner_rect(geom)
        return inner.contains(self.frameGeometry().center())

    def _effective_edge(self) -> Optional[int]:
        if self._snap_edge is not None:
            return self._snap_edge
        return self._taskbar_edge()

    def _travel_distance(self, geom, edge: Optional[int], ww: int, hh: int) -> float:
        if edge == ABE_LEFT or edge == ABE_RIGHT:
            return float(max(0, geom.height() - hh))
        return float(max(0, geom.width() - ww))

    def _start_walk_segment(self, travel: float) -> None:
        wt = self._walk_params()
        if self.pos_progress > 0.85:
            self._going_right = False
        elif self.pos_progress < 0.15:
            self._going_right = True
        else:
            self._going_right = random.choice([True, False])

        self._walk_start_pos = self.pos_progress
        ref = 500.0
        pixels = random_walk_amount(wt) * ref
        walk_amount = (pixels / travel) if travel > 0 else 0.3
        if self._going_right:
            self._walk_end_pos = min(self._walk_start_pos + walk_amount, 1.0)
        else:
            self._walk_end_pos = max(self._walk_start_pos - walk_amount, 0.0)

        self._walk_start_pixel = self._walk_start_pos * travel
        self._walk_end_pixel = self._walk_end_pos * travel

        self._walk_start_time = time.monotonic()
        self._is_walking_segment = True
        self.direction = 1 if self._going_right else -1
        self._pixmap_cache.clear()
        self._last_pixmap_key = None

    def _enter_pause_between_walks(self, short: bool) -> None:
        self._is_walking_segment = False
        lo, hi = self._pause_range(short)
        self._pause_end_time = time.monotonic() + random.uniform(lo, hi)
        self.current_frame_idx = 0

    def _sprite_dimensions(self) -> Tuple[int, int]:
        lw = self.label.width()
        lh = self.label.height()
        ww = self.width()
        hh = self.height()
        w = max(ww, lw, 80)
        h = max(hh, lh, self.target_height)
        return w, h

    def _current_pixmap_alpha_insets(self) -> Tuple[int, int, int, int]:
        """Return transparent insets (left, top, right, bottom) of current frame."""
        pm = self.label.pixmap()
        if pm is None or pm.isNull():
            return (0, 0, 0, 0)
        key = int(pm.cacheKey())
        cached = self._alpha_inset_cache.get(key)
        if cached is not None:
            return cached

        img = pm.toImage()
        w = img.width()
        h = img.height()
        if w <= 0 or h <= 0:
            return (0, 0, 0, 0)

        min_x, min_y = w, h
        max_x, max_y = -1, -1
        for y in range(h):
            for x in range(w):
                if img.pixelColor(x, y).alpha() > 0:
                    if x < min_x:
                        min_x = x
                    if y < min_y:
                        min_y = y
                    if x > max_x:
                        max_x = x
                    if y > max_y:
                        max_y = y

        if max_x < min_x or max_y < min_y:
            insets = (0, 0, 0, 0)
        else:
            insets = (min_x, min_y, w - 1 - max_x, h - 1 - max_y)
        self._alpha_inset_cache[key] = insets
        return insets

    def _rotation_for_edge(self, edge: Optional[int]) -> int:
        """Rotate sprite so feet orientation matches the current edge rail."""
        if edge == ABE_TOP:
            return 180
        if edge == ABE_LEFT:
            return 90
        if edge == ABE_RIGHT:
            return -90
        return 0

    def _frame_direction_for_edge(self, edge: Optional[int]) -> int:
        """Keep the sprite facing along the actual walk direction on each rail."""
        if edge in (ABE_TOP, ABE_RIGHT):
            return -self.direction
        return self.direction

    def update_frame(self) -> None:
        n = self._walk_frame_count()
        if n == 0:
            return
        idx = max(0, min(n - 1, self.current_frame_idx))
        edge = self._effective_edge()
        rotation = self._rotation_for_edge(edge)
        frame_direction = self._frame_direction_for_edge(edge)

        if self._mem_walk_pixmaps is not None:
            base = self._mem_walk_pixmaps[idx]
            cache_key = ("mem", idx, frame_direction, rotation)
            if cache_key not in self._pixmap_cache:
                scaled = base.scaledToHeight(self.target_height, Qt.TransformationMode.SmoothTransformation)
                if frame_direction < 0:
                    scaled = scaled.transformed(QTransform().scale(-1, 1))
                if rotation:
                    scaled = scaled.transformed(QTransform().rotate(rotation))
                self._pixmap_cache[cache_key] = scaled
            scaled_pixmap = self._pixmap_cache[cache_key]
        else:
            frame_path = self._disk_walk_paths[idx]
            cache_key = ("disk", frame_path, frame_direction, rotation)
            if cache_key not in self._pixmap_cache:
                pixmap = QPixmap(frame_path)
                if pixmap.isNull():
                    raise RuntimeError(f"Failed to load sprite frame: {frame_path}")
                scaled_pixmap = pixmap.scaledToHeight(
                    self.target_height, Qt.TransformationMode.SmoothTransformation
                )
                if frame_direction < 0:
                    scaled_pixmap = scaled_pixmap.transformed(QTransform().scale(-1, 1))
                if rotation:
                    scaled_pixmap = scaled_pixmap.transformed(QTransform().rotate(rotation))
                self._pixmap_cache[cache_key] = scaled_pixmap
            scaled_pixmap = self._pixmap_cache[cache_key]

        key: Tuple[str, int, int, int] = (
            "mem" if self._mem_walk_pixmaps is not None else "disk",
            idx,
            frame_direction,
            rotation,
        )
        if key != self._last_pixmap_key:
            self._last_pixmap_key = key
            self.label.setPixmap(scaled_pixmap.copy())
            self.label.adjustSize()
            self.resize(self.label.size())
            self.label.repaint()
            self.repaint()

    def next_frame(self) -> None:
        now = time.monotonic()
        dt = min(0.1, max(0.0, now - self._last_tick))
        self._last_tick = now

        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        if self._character_user_placed:
            self.current_frame_idx = 0
            self.direction = 1 if self._going_right else -1
            self.update_frame()
            self.update_position()
            return
        geom = screen.availableGeometry()
        w, h = self._sprite_dimensions()
        edge = self._effective_edge()
        travel = self._travel_distance(geom, edge, w, h)

        if self.is_paused:
            self.current_frame_idx = 0
            self.direction = 1 if self._going_right else -1
            self.update_frame()
            self.update_position()
            return

        if not self._is_walking_segment:
            if now >= self._pause_end_time and travel > 0:
                self._start_walk_segment(travel)
            self.current_frame_idx = 0
            self.direction = 1 if self._going_right else -1
            self.update_frame()
            self.update_position()
            return

        elapsed = now - self._walk_start_time
        walk_duration = self._walk_duration()
        scaled_video_time = min(self.VIDEO_DURATION, (elapsed / walk_duration) * self.VIDEO_DURATION)
        vt = scaled_video_time
        walk_norm = 1.0 if elapsed >= walk_duration else self._movement_at(vt)
        current_pixel = self._walk_start_pixel + (self._walk_end_pixel - self._walk_start_pixel) * walk_norm
        if travel > 0:
            self.pos_progress = max(0.0, min(1.0, current_pixel / travel))

        n = self._walk_frame_count()
        if n > 1:
            tnorm = max(0.0, min(1.0, elapsed / walk_duration))
            if self.character_name == "MrCat":
                # Match sprite to taskbar: no walk cycle during movement_position dead zone
                # (walk_norm stays 0); frames scrub only as the character actually slides.
                self.current_frame_idx = max(0, min(n - 1, int(round(walk_norm * (n - 1)))))
            else:
                self.current_frame_idx = int(round(tnorm * (n - 1)))
        elif n == 1:
            self.current_frame_idx = 0

        self.direction = 1 if self._going_right else -1

        if elapsed >= walk_duration:
            self._enter_pause_between_walks(short=False)

        self.update_frame()
        self.update_position()

    def _taskbar_rect(self):
        """Return the *taskbar's* QRect in screen pixels (None when unknown)."""
        if sys.platform != "win32" or not get_taskbar_info:
            return None
        info = get_taskbar_info()
        if not info:
            return None
        l, t, r, b = info["rect"]
        return QRect(int(l), int(t), int(r - l), int(b - t))

    def update_position(self) -> None:
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return

        geom = screen.availableGeometry()
        w, h = self._sprite_dimensions()
        edge = self._effective_edge()
        wt = self._walk_params()
        # No extra padding: sit exactly on the edge rail.
        rail_gap = 0
        y_off = self._scale_mac_px(float(wt.y_offset_px))
        flip_x = 0 if self._going_right else self._scale_mac_px(float(wt.flip_x_offset_px))
        inset_l, inset_t, inset_r, inset_b = self._current_pixmap_alpha_insets()

        if edge == ABE_LEFT:
            vertical_room = max(0, geom.height() - h)
            x = geom.left() + rail_gap - inset_l
            y = geom.top() + int(self.pos_progress * vertical_room) + y_off
        elif edge == ABE_RIGHT:
            vertical_room = max(0, geom.height() - h)
            x = geom.right() - w + 1 - rail_gap + inset_r
            y = geom.top() + int(self.pos_progress * vertical_room) + y_off
        elif edge == ABE_TOP:
            horizontal_room = max(0, geom.width() - w)
            x = geom.left() + int(self.pos_progress * horizontal_room) + flip_x
            y = geom.top() + rail_gap - inset_t + y_off
        else:
            # BOTTOM (default): keep character above taskbar via availableGeometry.
            horizontal_room = max(0, geom.width() - w)
            x = geom.left() + int(self.pos_progress * horizontal_room) + flip_x
            y = geom.bottom() - h + 1 - rail_gap + inset_b + y_off

        if self._character_user_placed:
            self._sync_overlay_positions()
            return

        # Clamp by visible (non-transparent) sprite extents.
        max_y = geom.bottom() - h + 1 + inset_b
        min_y = geom.top() - inset_t
        if y > max_y or y < min_y:
            y = max(min_y, min(y, max_y))
        max_x = geom.right() - w + 1 + inset_r
        min_x = geom.left() - inset_l
        if x > max_x or x < min_x:
            x = max(min_x, min(x, max_x))

        self.move(x, y)
        self._sync_overlay_positions()

    def _hit_test_opaque_character(self, pos) -> bool:
        pos_in_label = self.label.mapFrom(self, pos)
        pixmap = self.label.pixmap()
        if pixmap is None:
            return False
        if not self.label.rect().contains(pos_in_label):
            return False
        image = pixmap.toImage()
        if (
            pos_in_label.x() < 0
            or pos_in_label.y() < 0
            or pos_in_label.x() >= image.width()
            or pos_in_label.y() >= image.height()
        ):
            return False
        return image.pixelColor(pos_in_label).alpha() > 0

    def _clamp_character_to_screen(self) -> None:
        geo = self.frameGeometry()
        screen = QGuiApplication.screenAt(geo.center()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        x = max(avail.left(), min(geo.x(), avail.right() - geo.width() + 1))
        y = max(avail.top(), min(geo.y(), avail.bottom() - geo.height() + 1))
        if x != geo.x() or y != geo.y():
            self.move(x, y)

    def _release_char_mouse_grab(self) -> None:
        if self._char_grabbed_mouse:
            self._char_grabbed_mouse = False
            self.releaseMouse()

    def _snap_to_nearest_edge(self) -> None:
        """After a drag, stick to the nearest work-area edge and map progress along that rail."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            self._character_user_placed = False
            return
        geom = screen.availableGeometry()
        w, h = self._sprite_dimensions()
        wt = self._walk_params()
        y_off = self._scale_mac_px(float(wt.y_offset_px))
        flip_x = 0 if self._going_right else self._scale_mac_px(float(wt.flip_x_offset_px))

        cx = self.frameGeometry().center().x()
        cy = self.frameGeometry().center().y()
        candidates = [
            (cx - geom.left(), 0, ABE_LEFT),
            (cy - geom.top(), 1, ABE_TOP),
            (geom.right() - cx, 2, ABE_RIGHT),
            (geom.bottom() - cy, 3, ABE_BOTTOM),
        ]
        chosen = min(candidates, key=lambda t: (t[0], t[1]))[2]
        self._snap_edge = chosen

        if chosen == ABE_LEFT or chosen == ABE_RIGHT:
            vertical_room = max(1, geom.height() - h)
            raw = (self.y() - geom.top() - y_off) / float(vertical_room)
        else:
            horizontal_room = max(1, geom.width() - w)
            raw = (self.x() - geom.left() - flip_x) / float(horizontal_room)

        self.pos_progress = max(0.0, min(1.0, float(raw)))
        self._character_user_placed = False

    def _snap_terminal_near_character(self) -> None:
        self._terminal_follows_character = True
        if not self.terminal.isVisible():
            return
        self.terminal.move_for_rect(self._anchor_rect(), self._effective_edge())

    def _sync_overlay_positions(self) -> None:
        edge = self._effective_edge()
        character_anchor = self._anchor_rect()

        if self.terminal.isVisible() and self._terminal_follows_character:
            self.terminal.move_for_rect(character_anchor, edge)

        if self.thinking_bubble.isVisible():
            # Keep the thinking bubble attached to the character, not the
            # chat window. When it anchored to the terminal geometry it
            # could jump sideways / above the popover whenever the user
            # moved chat around, which felt wrong — the bubble is the
            # character "thinking", so it should stay over the character.
            self.thinking_bubble.move_for_rect(character_anchor, edge)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)

        if not self._hit_test_opaque_character(event.pos()):
            return super().mousePressEvent(event)

        self._char_drag_candidate = True
        self._char_dragging = False
        g = event.globalPosition().toPoint()
        self._char_press_global = g
        self._char_drag_offset = g - self.frameGeometry().topLeft()
        event.accept()

    def mouseMoveEvent(self, event):
        if (
            self._char_drag_candidate
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            g = event.globalPosition().toPoint()
            if not self._char_dragging:
                delta = g - self._char_press_global
                if abs(delta.x()) <= 6 and abs(delta.y()) <= 6:
                    return super().mouseMoveEvent(event)
                self._char_dragging = True
                self._character_user_placed = True
                self.is_paused = True
                self._is_walking_segment = False
                if not self._char_grabbed_mouse:
                    self._char_grabbed_mouse = True
                    self.grabMouse()
            self.move(g - self._char_drag_offset)
            self._clamp_character_to_screen()
            self._sync_overlay_positions()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._char_drag_candidate:
            if self._char_dragging:
                self._release_char_mouse_grab()
                self._clamp_character_to_screen()
                if self._character_center_in_free_placement_zone():
                    self._snap_edge = None
                    self._character_user_placed = True
                else:
                    self._snap_to_nearest_edge()
                self._is_walking_segment = False
                lo, hi = self._pause_range(short=True)
                self._pause_end_time = time.monotonic() + random.uniform(lo, hi)
                if not self.terminal.isVisible():
                    self.is_paused = False
                self.update_position()
                self._sync_overlay_positions()
            else:
                self.toggle_terminal()
            self._char_drag_candidate = False
            self._char_dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _prepare_terminal_for_display(self) -> None:
        self.thinking_bubble.hide_bubble()
        self.is_paused = True
        self._is_walking_segment = False
        self.current_frame_idx = 0
        self.terminal.set_context(self.provider_name, self.character_name)
        self.terminal.set_busy_state(bool(self.session and self.session.is_busy))
        self._refresh_linkable_sessions()
        if self._linked_session_id != "app":
            self._sync_linked_session_transcript()
        else:
            self._ensure_session()
            self.terminal.set_input_enabled(True)
            if self.terminal.chat_display.document().isEmpty():
                self.terminal.append_message("System", f"{self.provider_name} ready.")
            self._async_refresh_completions()

    def toggle_terminal(self) -> None:
        if self.terminal.isVisible():
            self.terminal.hide()
        else:
            self._terminal_follows_character = True
            self._prepare_terminal_for_display()
            self.terminal.show_for_rect(self._anchor_rect(), self._effective_edge())

    def open_terminal_near_cursor_from_tray(self) -> None:
        """Tray / double-click: open chat near the pointer (Windows); use /resetpos to dock on the walker."""
        self._terminal_follows_character = False
        self._prepare_terminal_for_display()
        # PyQt6 dropped the QGuiApplication.cursor() shim that Qt5 had; the
        # pointer position now lives on ``QCursor.pos()``.
        self.terminal.show_at(QCursor.pos(), self._effective_edge())

    def open_terminal_near_walker_auto(self) -> None:
        """Auto-open the chat popover docked on the walker.

        Used by the session sync when a new external CLI session is detected
        so the user gets a control window without double-clicking. Silently
        skipped if the popover is already visible.
        """
        if self.terminal.isVisible():
            return
        if not self.isVisible():
            return
        self._terminal_follows_character = True
        self._prepare_terminal_for_display()
        self.terminal.show_for_rect(self._anchor_rect(), self._effective_edge())

    def _on_terminal_closed(self) -> None:
        self._terminal_follows_character = True
        if not self.is_paused:
            return
        self.is_paused = False
        self._is_walking_segment = False
        self.current_frame_idx = 0
        self._enter_pause_between_walks(short=True)
        if self._pending_completion:
            self._pending_completion = False
            self.thinking_bubble.show_completion_for_rect(self._anchor_rect(), self._effective_edge())
        elif self.session is not None and self.session.is_busy:
            self.thinking_bubble.show_for_rect(self._anchor_rect(), self._effective_edge())

    def set_busy(self, busy: bool) -> None:
        self.terminal.set_busy_state(busy)
        if busy and not self._was_busy:
            self._external_cli_active = False
            if not self.terminal.isVisible():
                self.thinking_bubble.show_for_rect(self._anchor_rect(), self._effective_edge())
        elif not busy and self._was_busy:
            if not self._external_cli_active:
                self.thinking_bubble.hide_bubble()
        self._was_busy = busy

    def _on_turn_completed(self) -> None:
        self.terminal.commit_assistant_turn()
        app = self._host_app()
        relay = getattr(app, "handle_walker_turn_completed", None)
        if callable(relay) and self.walker_id:
            relay(self.walker_id, self.last_assistant_text())
        self.sound_manager.play_done()
        if self.terminal.isVisible():
            self._pending_completion = True
            return
        self._pending_completion = False
        self.thinking_bubble.show_completion_for_rect(self._anchor_rect(), self._effective_edge())

    def closeEvent(self, event) -> None:
        self._release_char_mouse_grab()
        self._teardown_session()
        app = self._host_app()
        unregister = getattr(app, "unregister_walker", None)
        if callable(unregister) and self.walker_id:
            unregister(self.walker_id)
        cb = self._on_aux_closed
        if cb:
            self._on_aux_closed = None
            cb()
        super().closeEvent(event)

    # ---- perf: pause the 30 fps animation timer while we're hidden ------
    # The walker is invisible in "defer until external CLI" mode for most
    # of the session; repainting off-screen sprites every 33 ms is pure
    # waste. We also reset the monotonic tick so the first visible frame
    # after re-show doesn't race through a big delta.
    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._last_tick = time.monotonic()
        if hasattr(self, "timer") and not self.timer.isActive():
            self.timer.start(self._animation_interval_ms)

    def hideEvent(self, event) -> None:  # type: ignore[override]
        super().hideEvent(event)
        if hasattr(self, "timer") and self.timer.isActive():
            self.timer.stop()
