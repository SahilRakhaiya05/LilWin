import logging
import os
import sys
from dataclasses import dataclass
from functools import partial
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QApplication, QDialog, QSystemTrayIcon

from __version__ import VERSION
from services.character_registry import get_registry
from services.provider_factory import PROVIDER_SPECS
from services.walker_channels import WalkerChannelHub, WalkerChannelState
from ui.character_window import CharacterWindow
from ui.settings_dialog import SettingsDialog
from ui.system_tray import LilAgentsTray
from utils.config import ConfigManager
from utils.terminal_sessions import list_all_active_provider_sessions as list_active_integrated_provider_sessions
from utils.logging_config import setup_logging

logger = logging.getLogger(__name__)


@dataclass
class WalkerRuntimeState:
    walker_id: str
    character_name: str
    provider_name: str


class LilAgentsApp(QApplication):
    _SESSION_WALKER_REFRESH_MS = 3000

    def __init__(self, argv):
        super().__init__(argv)
        self.setQuitOnLastWindowClosed(False)
        self._tray_external_cli_hint_shown = False
        self._last_notified_session_key: Optional[str] = None
        self.config = ConfigManager()
        self.settings = self.config.load()
        self._default_provider = str(self.settings.get("provider", "Claude"))
        self.registry = get_registry()
        self._ensure_provider_character_map()
        self.channel_hub = WalkerChannelHub()
        self._walker_runtime: Dict[str, WalkerRuntimeState] = {}

        initial_character = self._resolve_initial_character()
        self.character_window = CharacterWindow(
            initial_character,
            provider_name=self._default_provider,
            walker_id="primary",
        )
        self._extra_walkers: dict[str, CharacterWindow] = {}
        self._session_character_assignments: Dict[str, str] = {}
        self._register_walker_runtime("primary", self.character_window)

        self.tray = LilAgentsTray(self)
        self.tray.provider_changed.connect(self._tray_set_provider)
        self.tray.character_changed.connect(self._on_tray_character)
        self.tray.character_toggled.connect(self._on_tray_character_toggled)
        self.tray.theme_changed.connect(self._tray_set_theme)
        self.tray.open_chat_requested.connect(self._open_chat_from_tray)
        self.tray.spawn_walker_requested.connect(self._on_tray_spawn_walker)
        self.tray.settings_action.triggered.connect(self._open_settings)

        self._session_walker_timer = QTimer(self)
        self._session_walker_timer.timeout.connect(self._sync_session_walker_slots)
        self._session_walker_timer.start(self._SESSION_WALKER_REFRESH_MS)

        self.aboutToQuit.connect(self._on_about_to_quit)

        self._sync_tray_from_settings()
        # Show the walker on startup unless the user configured
        # ``characterVisibilityMode`` to wait for an external CLI
        # (``poll_external_cli_once`` will then show it when it detects
        # one). The previous check gated on ``isVisible()``, which is
        # False before the very first ``show()`` — so the walker never
        # appeared and the app looked tray-only.
        try:
            defer = self.character_window.user_must_start_external_terminal_first()
            logger.debug(
                "startup: defer_until_cli=%s visibility_mode=%s",
                defer,
                self.character_window._character_visibility_mode(),
            )
            if not defer:
                self.character_window.present_on_desktop()
                logger.debug(
                    "startup: present_on_desktop done; visible=%s pos=%s",
                    self.character_window.isVisible(),
                    self.character_window.pos(),
                )
        except Exception:
            logger.exception("startup present failed")
        self.character_window.poll_external_cli_once()
        self._sync_session_walker_slots()

        logger.info("LilWin %s ready (provider=%s, character=%s)",
                    VERSION, self.settings.get("provider"), initial_character)

    def _resolve_initial_character(self) -> str:
        preferred = str(self.settings.get("character", "Bruce")).strip().lower()
        if self.registry.get(preferred):
            return preferred
        provider = str(self.settings.get("provider", "Claude"))
        mapped = self.registry.default_for_provider(provider)
        if mapped:
            return mapped.name
        roster = self.registry.names()
        return roster[0] if roster else "bruce"

    def _ensure_provider_character_map(self) -> None:
        """Populate settings[providerCharacterMap] from registry defaults if empty."""
        mapping = dict(self.settings.get("providerCharacterMap") or {})
        defaults = self.registry.provider_character_map()
        changed = False
        for provider, char_name in defaults.items():
            if provider not in mapping:
                mapping[provider] = char_name
                changed = True
        if changed:
            self.settings["providerCharacterMap"] = mapping
            self.config.save(self.settings)

    def _default_provider_name(self) -> str:
        return self._default_provider or "Claude"

    def _walker_by_id(self, walker_id: str) -> Optional[CharacterWindow]:
        if walker_id == "primary":
            return self.character_window
        return self._extra_walkers.get(walker_id)

    def _register_walker_runtime(self, walker_id: str, walker: CharacterWindow) -> None:
        self._walker_runtime[walker_id] = WalkerRuntimeState(
            walker_id=walker_id,
            character_name=walker.character_name,
            provider_name=walker.provider_name,
        )
        state = self.channel_hub.register_walker(walker_id)
        walker.set_channel_state(
            channel_id=state.channel_id,
            auto_collab=state.auto_collab,
            collab_role=state.collab_role,
            max_rounds=state.max_rounds,
            collab_partner_id=state.collab_partner_id,
        )

    def unregister_walker(self, walker_id: str) -> None:
        self._walker_runtime.pop(walker_id, None)
        self.channel_hub.unregister_walker(walker_id)

    def _sync_channel_state_to_walker(self, walker_id: str) -> None:
        walker = self._walker_by_id(walker_id)
        if not walker:
            return
        state = self.channel_hub.state_for(walker_id)
        walker.set_channel_state(
            channel_id=state.channel_id,
            auto_collab=state.auto_collab,
            collab_role=state.collab_role,
            max_rounds=state.max_rounds,
            collab_partner_id=state.collab_partner_id,
        )

    def set_default_provider(self, provider_name: str) -> None:
        if provider_name not in PROVIDER_SPECS:
            return
        self._default_provider = provider_name
        self.settings = self.config.load()
        self.settings["provider"] = provider_name
        self.config.save(self.settings)
        self._sync_tray_provider_checks()

    def set_walker_provider(self, walker_id: str, provider_name: str) -> None:
        if provider_name not in PROVIDER_SPECS:
            return
        walker = self._walker_by_id(walker_id)
        if not walker:
            return
        walker.set_provider(provider_name)
        runtime = self._walker_runtime.get(walker_id)
        if runtime:
            runtime.provider_name = provider_name

    def join_walker_channel(self, walker_id: str, channel_id: str) -> str:
        state = self.channel_hub.set_channel(walker_id, channel_id)
        self._sync_channel_state_to_walker(walker_id)
        return state.channel_id or ""

    def leave_walker_channel(self, walker_id: str) -> None:
        self.channel_hub.leave_channel(walker_id)
        self._sync_channel_state_to_walker(walker_id)

    def channel_state_for_walker(self, walker_id: str) -> WalkerChannelState:
        return self.channel_hub.state_for(walker_id)

    def channel_members_for_walker(self, walker_id: str) -> List[str]:
        state = self.channel_hub.state_for(walker_id)
        members = self.channel_hub.walker_channel_members(walker_id)
        out: List[str] = []
        for member_id in members:
            runtime = self._walker_runtime.get(member_id)
            walker = self._walker_by_id(member_id)
            label = walker.walker_display_name() if walker else (runtime.character_name if runtime else member_id)
            provider = runtime.provider_name if runtime else (walker.provider_name if walker else "?")
            mark = " (auto)" if self.channel_hub.state_for(member_id).auto_collab else ""
            out.append(f"{label} [{provider}]{mark}")
        return out

    def configure_walker_collaboration(
        self,
        walker_id: str,
        *,
        enabled: bool,
        role: str = "participant",
        max_rounds: int = 4,
    ) -> WalkerChannelState:
        buddy: Optional[str] = None
        if not enabled:
            buddy = self.channel_hub.state_for(walker_id).collab_partner_id
        state = self.channel_hub.configure_collaboration(
            walker_id,
            enabled=enabled,
            role=role,
            max_rounds=max_rounds,
        )
        self._sync_channel_state_to_walker(walker_id)
        if buddy:
            self._sync_channel_state_to_walker(buddy)
        return state

    def resolve_walker_id_by_character_label(self, label: str) -> Optional[str]:
        key = (label or "").strip()
        if not key:
            return None
        kl = key.lower()

        def _check(wid: str, w: CharacterWindow) -> bool:
            if w.character_name.strip().lower() == kl:
                return True
            if w.walker_display_name().strip().lower() == kl:
                return True
            if wid.strip().lower() == kl:
                return True
            return False

        if _check("primary", self.character_window):
            return "primary"
        for wid, w in self._extra_walkers.items():
            if _check(wid, w):
                return wid
        return None

    def bind_walker_collab_partner(self, walker_id: str, partner_label: str) -> tuple[Optional[str], str]:
        pid = self.resolve_walker_id_by_character_label(partner_label)
        if not pid:
            return None, f"No walker matched {partner_label!r}. Check the character name or spawn them first."
        if pid == walker_id:
            return None, "Pick a different walker as your collaboration partner."
        st = self.channel_hub.state_for(walker_id)
        if not st.channel_id:
            return None, "Join a channel first (`/channel join <name>` on this walker)."
        other = self.channel_hub.state_for(pid)
        if other.channel_id != st.channel_id:
            return (
                None,
                f"That walker is not in channel `{st.channel_id}` — join the same channel on both walkers.",
            )
        self.channel_hub.set_collab_partner(walker_id, pid)
        self._sync_channel_state_to_walker(walker_id)
        self._sync_channel_state_to_walker(pid)
        return pid, ""

    def clear_walker_collab_partner(self, walker_id: str) -> None:
        buddy = self.channel_hub.state_for(walker_id).collab_partner_id
        self.channel_hub.set_collab_partner(walker_id, None)
        self._sync_channel_state_to_walker(walker_id)
        if buddy:
            self._sync_channel_state_to_walker(buddy)

    def walker_collab_partner_display(self, walker_id: str) -> str:
        st = self.channel_hub.state_for(walker_id)
        pid = st.collab_partner_id
        if not pid:
            return ""
        w = self._walker_by_id(pid)
        return w.walker_display_name() if w else pid

    def relay_walker_channel_message(
        self, from_walker_id: str, target_label: str, message: str
    ) -> tuple[bool, str]:
        """Deliver a line to exactly one other walker's provider (same channel)."""
        msg = (message or "").strip()
        if not msg:
            return False, "Message is empty."
        tid = self.resolve_walker_id_by_character_label(target_label)
        if not tid:
            return False, f"No walker matched {target_label!r}."
        if tid == from_walker_id:
            return False, "Pick a different walker to /tell."
        st = self.channel_hub.state_for(from_walker_id)
        if not st.channel_id:
            return False, "Join a channel first (`/channel join <name>`)."
        if self.channel_hub.state_for(tid).channel_id != st.channel_id:
            return (
                False,
                f"That walker is not in channel `{st.channel_id}`.",
            )
        target = self._walker_by_id(tid)
        source = self._walker_by_id(from_walker_id)
        runtime = self._walker_runtime.get(from_walker_id)
        if not target or not source or not runtime:
            return False, "Walker not available."
        ok = target.receive_channel_prompt(
            channel_id=st.channel_id,
            source_label=source.walker_display_name(),
            source_provider=runtime.provider_name,
            prompt_text=msg,
            auto_collab=False,
            source_cwd=source.get_cli_cwd(),
        )
        if not ok:
            return (
                False,
                "Target could not accept the prompt (linked external terminal or no session).",
            )
        return True, ""

    def handle_walker_user_message(self, walker_id: str, text: str) -> None:
        state = self.channel_hub.state_for(walker_id)
        if not state.channel_id:
            return
        source = self._walker_by_id(walker_id)
        runtime = self._walker_runtime.get(walker_id)
        if not source or not runtime:
            return
        if state.auto_collab:
            self.channel_hub.start_collaboration(walker_id, text)
            for member_id in self.channel_hub.walker_channel_members(walker_id):
                if member_id == walker_id:
                    continue
                target = self._walker_by_id(member_id)
                if target:
                    target.terminal.append_message(
                        "System",
                        f"[channel {state.channel_id}] Auto-collab started by {source.walker_display_name()}.",
                    )
            return
        # Manual channel mode: user text goes only to this walker's provider.
        # Peers see shared context via assistant replies (append_channel_response)
        # and optional `/tell` — never duplicate the same user line to every member.

    def handle_walker_turn_completed(self, walker_id: str, response_text: str) -> None:
        if not response_text.strip():
            return
        state = self.channel_hub.state_for(walker_id)
        if not state.channel_id:
            return
        source = self._walker_by_id(walker_id)
        runtime = self._walker_runtime.get(walker_id)
        if not source or not runtime:
            return
        members = self.channel_hub.walker_channel_members(walker_id)
        for member_id in members:
            if member_id == walker_id:
                continue
            target = self._walker_by_id(member_id)
            if target:
                target.append_channel_response(
                    state.channel_id,
                    source.walker_display_name(),
                    response_text,
                    source_cwd=source.get_cli_cwd(),
                )
        collab = self.channel_hub.active_collaboration_for_walker(walker_id)
        if not collab or collab.channel_id != state.channel_id:
            return
        next_id = self.channel_hub.next_collaboration_target(state.channel_id, walker_id)
        if not next_id:
            self.channel_hub.stop_collaboration(state.channel_id)
            return
        target = self._walker_by_id(next_id)
        if not target:
            self.channel_hub.stop_collaboration(state.channel_id)
            return
        ok = target.receive_channel_prompt(
            channel_id=state.channel_id,
            source_label=source.walker_display_name(),
            source_provider=runtime.provider_name,
            prompt_text=response_text,
            auto_collab=True,
            collab_goal=collab.goal,
            round_index=collab.round_index,
            max_rounds=collab.max_rounds,
            source_cwd=source.get_cli_cwd(),
        )
        if not ok:
            self.channel_hub.stop_collaboration(state.channel_id)

    def _roster(self) -> List[str]:
        names = self.registry.names()
        return names or ["bruce"]

    def _assign_session_character(self, session_key: str, used: set) -> str:
        prior = self._session_character_assignments.get(session_key)
        roster = self._roster()
        if prior and prior in roster and prior not in used:
            used.add(prior)
            return prior
        for candidate in roster:
            if candidate not in used:
                used.add(candidate)
                self._session_character_assignments[session_key] = candidate
                return candidate
        # Roster exhausted — allow duplicate but log once.
        logger.info("More sessions (%d) than characters (%d); reusing %s",
                    len(used) + 1, len(roster), roster[0])
        used.add(roster[0])
        self._session_character_assignments[session_key] = roster[0]
        return roster[0]

    def _close_all_extra_walkers(self) -> None:
        for w in list(self._extra_walkers.values()):
            w._on_aux_closed = None
            w.close()
        self._extra_walkers.clear()

    def _on_extra_walker_closed(self, session_key: str) -> None:
        self._extra_walkers.pop(session_key, None)
        self.unregister_walker(session_key)

    def _close_extra_walkers_matching(self, predicate) -> None:
        for key in list(self._extra_walkers.keys()):
            if not predicate(key):
                continue
            w = self._extra_walkers.pop(key, None)
            if w:
                w._on_aux_closed = None
                w.close()

    def _sync_idle_roster_walkers(self, enabled: bool) -> None:
        """Show one idle walker per character (except the primary one)."""
        if not enabled:
            self._close_extra_walkers_matching(lambda key: key.startswith("idle:"))
            return

        primary = self.character_window
        roster = [n for n in self._roster() if n != primary.character_name]
        want_keys = {f"idle:{name}" for name in roster}
        self._close_extra_walkers_matching(
            lambda key: key.startswith("idle:") and key not in want_keys
        )

        for i, name in enumerate(roster, start=1):
            key = f"idle:{name}"
            if key in self._extra_walkers:
                w = self._extra_walkers[key]
                w.apply_saved_settings(self.settings)
                continue
            try:
                w = CharacterWindow(
                    name,
                    provider_name=self._default_provider_name(),
                    walker_id=key,
                    on_aux_closed=partial(self._on_extra_walker_closed, key),
                )
                # Same pin logic as tray/user-spawn walkers — idle-roster
                # walkers represent a specific character and must not be
                # swapped by provider-character mapping.
                w._character_manually_pinned = True
                w.apply_saved_settings(self.settings)
                w.set_provider(self._default_provider_name())
                self._extra_walkers[key] = w
                self._register_walker_runtime(key, w)
                w.present_on_desktop()
                geo = primary.frameGeometry()
                w.move(geo.left() + min(72 * i, 480), geo.top())
                w._clamp_character_to_screen()
            except Exception:
                logger.exception("idle roster walker spawn failed: %s", name)

    def _sync_session_walker_slots(self) -> None:
        self.settings = self.config.load()
        primary = self.character_window
        def _is_user_controlled(key: str) -> bool:
            return (
                key.startswith("user-spawn:")
                or key.startswith("idle:")
                or key.startswith("tray-char:")
            )

        if not bool(self.settings.get("showCursorTerminalLink", False)):
            primary.set_pinned_integrated_session(None)
            # Keep user-spawn, tray-char and optional idle-roster walkers;
            # tear down only auto terminal-session walkers when linking is off.
            self._close_extra_walkers_matching(lambda key: not _is_user_controlled(key))
            self._sync_idle_roster_walkers(bool(self.settings.get("showIdleRosterCharacters", False)))
            self._refresh_tray_character_checks()
            return
        provider = primary.provider_name
        if provider == "OpenClaw":
            primary.set_pinned_integrated_session(None)
            self._close_extra_walkers_matching(lambda key: not _is_user_controlled(key))
            self._sync_idle_roster_walkers(bool(self.settings.get("showIdleRosterCharacters", False)))
            self._refresh_tray_character_checks()
            return

        repo = primary._repo_root()
        sessions = list_active_integrated_provider_sessions(repo, provider)
        if not sessions:
            # No external CLI sessions — unpin the primary walker and tear down
            # any automatic extras. Walker falls back to plain "app session" mode.
            primary.set_pinned_integrated_session(None)
            self._close_extra_walkers_matching(lambda key: not _is_user_controlled(key))
            self._sync_idle_roster_walkers(bool(self.settings.get("showIdleRosterCharacters", False)))
            self._refresh_tray_character_checks()
            return

        # External sessions are active; hide idle-only extras so per-session
        # walkers can represent live terminals cleanly.
        self._sync_idle_roster_walkers(False)

        # One or more active IDE sessions found.
        # • Pin the primary walker to the first session so transcript mirroring
        #   and the send-bridge are wired up.
        # • Bring the character to the foreground the first time a session is
        #   detected, but do NOT auto-open the chat window — the user can click
        #   the character or use the tray "Open Chat" action themselves.
        #   (Auto-open was the source of "chat pops up when I didn't ask for it".)
        # • Spawn a dedicated walker for every additional session.
        newly_pinned = (
            primary._pinned_integrated_session is None
            or primary._pinned_integrated_session.session_key != sessions[0].session_key
        )
        primary.set_pinned_integrated_session(sessions[0])
        if newly_pinned:
            try:
                primary.present_on_desktop()
                # Chat is intentionally NOT auto-opened here. The character
                # appears / walks on screen; the user opens chat by clicking it.
            except Exception:
                logger.debug("present_on_desktop failed", exc_info=True)
            # Show a tray toast once per unique session so the user knows the
            # character is now linked.  Reset per session key so a new session
            # always gets a notification even if one was shown before.
            new_key = sessions[0].session_key
            if self._last_notified_session_key != new_key:
                self._last_notified_session_key = new_key
                self.tray.showMessage(
                    "LilWin",
                    f"{sessions[0].source or provider} session detected — click the character to open chat.",
                    QSystemTrayIcon.MessageIcon.Information,
                    4000,
                )

        want_keys = {s.session_key for s in sessions[1:]}
        for key in list(self._extra_walkers.keys()):
            if key in want_keys:
                continue
            if _is_user_controlled(key):
                continue
            w = self._extra_walkers.pop(key, None)
            if w:
                w._on_aux_closed = None
                w.close()
        # Prune assignments that are no longer referenced.
        live_keys = {s.session_key for s in sessions}
        for stale in [k for k in self._session_character_assignments if k not in live_keys]:
            self._session_character_assignments.pop(stale, None)

        used = {primary.character_name}
        for i, sess in enumerate(sessions[1:], start=1):
            char = self._assign_session_character(sess.session_key, used)
            if sess.session_key in self._extra_walkers:
                w = self._extra_walkers[sess.session_key]
                if w.character_name != char:
                    w.set_character(char, manual=False)
                w.apply_saved_settings(self.settings)
            else:
                w = CharacterWindow(
                    char,
                    provider_name=provider,
                    walker_id=sess.session_key,
                    pinned_integrated_session=sess,
                    on_aux_closed=partial(self._on_extra_walker_closed, sess.session_key),
                )
                w.apply_saved_settings(self.settings)
                self._extra_walkers[sess.session_key] = w
                self._register_walker_runtime(sess.session_key, w)
                w.present_on_desktop()
                geo = primary.frameGeometry()
                w.move(geo.left() + min(72 * i, 420), geo.top())
                w._clamp_character_to_screen()
                # Extra walkers also don't auto-open — user clicks to open chat.
        self._refresh_tray_character_checks()

    def _spawn_extra_walker(self, character_name: str, provider_name: Optional[str] = None) -> bool:
        """Public hook for popover ``/spawn`` — stand up a second walker.

        The new walker is *not* bound to any external terminal; it just runs
        a second app-managed CLI session with the same provider as the
        primary walker. Returns True on success.
        """
        key = f"user-spawn:{character_name}:{len(self._extra_walkers)}"
        return self._spawn_extra_walker_with_key(character_name, key, provider_name)

    def _spawn_extra_walker_with_key(
        self,
        character_name: str,
        key: str,
        provider_name: Optional[str] = None,
    ) -> bool:
        """Spawn an extra walker under a caller-provided registry key."""
        primary = self.character_window
        chosen_provider = provider_name or self._default_provider_name()
        if key in self._extra_walkers:
            return True
        try:
            w = CharacterWindow(
                character_name,
                provider_name=chosen_provider,
                walker_id=key,
                on_aux_closed=partial(self._on_extra_walker_closed, key),
            )
            # Lock the character choice so later ``set_provider`` / settings
            # applies don't swap this walker to the provider's default
            # character. Without this the user sees "all walkers turn into
            # the same one" whenever they pick another character from the
            # tray (set_provider runs next and calls
            # ``_maybe_apply_provider_character`` which follows
            # ``providerCharacterMap``).
            w._character_manually_pinned = True
            w.apply_saved_settings(self.settings)
            w.set_provider(chosen_provider)
            self._extra_walkers[key] = w
            self._register_walker_runtime(key, w)
            w.present_on_desktop()
            geo = primary.frameGeometry()
            offset_idx = max(1, len(self._extra_walkers))
            w.move(geo.left() + min(72 * offset_idx, 420), geo.top())
            w._clamp_character_to_screen()
            return True
        except Exception:
            logger.exception("_spawn_extra_walker failed for %s", character_name)
            return False

    def _tray_char_key(self, character_name: str) -> str:
        return f"tray-char:{character_name.strip().lower()}"

    def _on_tray_character_toggled(self, name: str, checked: bool) -> None:
        """Character-submenu item toggled: add/remove an independent walker.

        The primary walker's character is locked (it always stays on-screen
        and always appears checked in the menu). Any other character becomes
        a second walker with its own CLI session when checked.
        """
        low = name.strip().lower()
        primary = self.character_window
        if low == primary.character_name:
            if not checked:
                self._set_tray_character_checked(low, True)
            return

        key = self._tray_char_key(low)
        if checked:
            if key in self._extra_walkers:
                return
            ok = self._spawn_extra_walker_with_key(low, key)
            if not ok:
                self._set_tray_character_checked(low, False)
                return
            try:
                display = self.registry.get(low).display_name if self.registry.get(low) else low
                self.tray.showMessage(
                    "LilWin",
                    f"Added walker: {display}. Runs its own {self._default_provider_name()} session.",
                    QSystemTrayIcon.MessageIcon.Information,
                    3000,
                )
            except Exception:
                logger.debug("tray-char toast failed", exc_info=True)
        else:
            w = self._extra_walkers.pop(key, None)
            if w:
                w._on_aux_closed = None
                w.close()

    def _set_tray_character_checked(self, name: str, checked: bool) -> None:
        action = self.tray.character_actions.get(name.strip().lower())
        if not action:
            return
        if action.isChecked() == checked:
            return
        action.blockSignals(True)
        try:
            action.setChecked(checked)
        finally:
            action.blockSignals(False)

    def _refresh_tray_character_checks(self) -> None:
        """Sync the Characters menu to match primary + active `tray-char:` walkers."""
        primary_name = self.character_window.character_name
        active = {primary_name}
        for key in self._extra_walkers:
            if key.startswith("tray-char:"):
                active.add(key.split(":", 1)[1])
        for low, action in self.tray.character_actions.items():
            self._set_tray_character_checked(low, low in active)

    def _sync_tray_provider_checks(self) -> None:
        selected_provider = self._default_provider_name()
        for action in self.tray.provider_group.actions():
            action.blockSignals(True)
        try:
            for action in self.tray.provider_group.actions():
                action.setChecked(action.text() == selected_provider)
        finally:
            for action in self.tray.provider_group.actions():
                action.blockSignals(False)

    def _on_tray_spawn_walker(self, character_name: str, provider_name: str) -> None:
        """Tray **Walkers → &lt;character&gt; → provider** — extra session for that character."""
        ok = self._spawn_extra_walker(character_name.strip().lower(), provider_name)
        if ok:
            try:
                char = self.registry.get(character_name)
                label = char.display_name if char else character_name
                self.tray.showMessage(
                    "LilWin",
                    f"Added walker: {label}. It runs its own {provider_name} session.",
                    QSystemTrayIcon.MessageIcon.Information,
                    3500,
                )
            except Exception:  # pragma: no cover - notification is best-effort
                logger.debug("spawn tray toast failed", exc_info=True)

    def _tray_set_provider(self, provider_name: str) -> None:
        self.set_default_provider(provider_name)
        try:
            self.tray.showMessage(
                "LilWin",
                f"Default provider for new walkers → {provider_name}. Use /provider in a walker's chat to switch that specific walker.",
                QSystemTrayIcon.MessageIcon.Information,
                3200,
            )
        except Exception:
            logger.debug("default provider toast failed", exc_info=True)

    def _tray_set_theme(self, theme_name: str) -> None:
        self.character_window.set_theme(theme_name)
        for w in self._extra_walkers.values():
            w.set_theme(theme_name)

    def _on_tray_character(self, name: str) -> None:
        w = self.character_window
        w.set_character(name.strip().lower(), manual=True)
        if not w.user_must_start_external_terminal_first():
            w.present_on_desktop()
        self._sync_session_walker_slots()

    def _open_chat_from_tray(self) -> None:
        w = self.character_window
        if w.user_must_start_external_terminal_first():
            if not self._tray_external_cli_hint_shown:
                self._tray_external_cli_hint_shown = True
                self.tray.showMessage(
                    "LilWin",
                    f"Start {w.provider_name} anywhere (IDE terminal, Windows Terminal, …) so the walker can appear, "
                    "or set Desktop character → Always show in Settings.",
                    QSystemTrayIcon.MessageIcon.Information,
                    6500,
                )
            return
        w.present_on_desktop()
        if not w.terminal.isVisible():
            w.open_terminal_near_cursor_from_tray()
        else:
            w.terminal.raise_()
            w.terminal.input_field.setFocus()

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.config, self.character_window)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.settings = self.config.load()
            self.character_window.apply_saved_settings(self.settings)
            for w in self._extra_walkers.values():
                w.apply_saved_settings(self.settings)
            # If user edited their provider-character map, re-apply it to
            # all walkers (non-pinned).
            for w in [self.character_window, *self._extra_walkers.values()]:
                w._maybe_apply_provider_character(w.provider_name)
            self._sync_session_walker_slots()

    def _sync_tray_from_settings(self):
        selected_provider = self.settings.get("provider", "Claude")
        selected_character = self.settings.get("character", "Bruce")
        selected_theme = self.settings.get("theme", "Peach")
        self._default_provider = str(selected_provider)

        groups = [
            self.tray.provider_group,
            self.tray.character_group,
            self.tray.theme_group,
        ]
        for g in groups:
            for a in g.actions():
                a.blockSignals(True)
        try:
            for action in self.tray.provider_group.actions():
                action.setChecked(action.text() == selected_provider)

            # The Characters menu is a multi-select toggle list now; tick
            # the primary character plus anything with an active tray-char walker.
            primary_low = str(selected_character).strip().lower()
            active = {primary_low}
            for key in self._extra_walkers:
                if key.startswith("tray-char:"):
                    active.add(key.split(":", 1)[1])
            for action in self.tray.character_group.actions():
                action.setChecked(action.text().strip().lower() in active)

            for action in self.tray.theme_group.actions():
                action.setChecked(action.text() == selected_theme)
        finally:
            for g in groups:
                for a in g.actions():
                    a.blockSignals(False)

        self.character_window.set_character(selected_character.strip().lower(), manual=True)
        self.character_window.set_theme(selected_theme)
        for w in self._extra_walkers.values():
            w.set_theme(selected_theme)

        self._sync_session_walker_slots()
        self._sync_tray_provider_checks()
        self._refresh_tray_character_checks()

    def _on_about_to_quit(self) -> None:
        """Runs when Qt starts tearing things down — shut down sessions cleanly."""
        try:
            self._close_all_extra_walkers()
            if self.character_window and self.character_window.session is not None:
                self.character_window._teardown_session()
        except Exception:
            logger.exception("Error during graceful shutdown")

    def quit(self):
        try:
            self._close_all_extra_walkers()
            self.character_window.close()
        finally:
            super().quit()


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    log_path = setup_logging()
    logger.info("LilWin starting; log file: %s", log_path)

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = LilAgentsApp(sys.argv)
    sys.exit(app.exec())
