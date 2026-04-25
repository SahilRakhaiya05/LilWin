from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class WalkerChannelState:
    walker_id: str
    channel_id: Optional[str] = None
    auto_collab: bool = False
    collab_role: str = "participant"
    max_rounds: int = 4
    # When set, auto-collab ping-pongs only between this walker and collab_partner_id.
    collab_partner_id: Optional[str] = None


@dataclass
class ActiveCollaboration:
    channel_id: str
    goal: str
    origin_walker_id: str
    last_speaker_id: Optional[str] = None
    round_index: int = 0
    max_rounds: int = 4
    active: bool = True
    # Narrow two-walker orchestration; alternates with origin_walker_id only.
    peer_walker_id: Optional[str] = None


class WalkerChannelHub:
    """Tracks channel membership and bounded collaboration state."""

    def __init__(self) -> None:
        self._walkers: Dict[str, WalkerChannelState] = {}
        self._channels: Dict[str, List[str]] = {}
        self._collabs: Dict[str, ActiveCollaboration] = {}

    def register_walker(self, walker_id: str) -> WalkerChannelState:
        state = self._walkers.get(walker_id)
        if state is None:
            state = WalkerChannelState(walker_id=walker_id)
            self._walkers[walker_id] = state
        return state

    def unregister_walker(self, walker_id: str) -> None:
        self.set_collab_partner(walker_id, None)
        state = self._walkers.pop(walker_id, None)
        if state and state.channel_id:
            self.leave_channel(walker_id)
        for channel_id, collab in list(self._collabs.items()):
            if collab.origin_walker_id == walker_id or collab.last_speaker_id == walker_id:
                self._collabs.pop(channel_id, None)

    def state_for(self, walker_id: str) -> WalkerChannelState:
        return self.register_walker(walker_id)

    def set_channel(self, walker_id: str, channel_id: Optional[str]) -> WalkerChannelState:
        state = self.register_walker(walker_id)
        channel_id = (channel_id or "").strip().lower() or None
        if state.channel_id == channel_id:
            return state
        self.set_collab_partner(walker_id, None)
        if state.channel_id:
            old_members = self._channels.get(state.channel_id, [])
            self._channels[state.channel_id] = [wid for wid in old_members if wid != walker_id]
            collab = self._collabs.get(state.channel_id)
            if collab and (collab.origin_walker_id == walker_id or collab.last_speaker_id == walker_id):
                self._collabs.pop(state.channel_id, None)
            if not self._channels[state.channel_id]:
                self._channels.pop(state.channel_id, None)
                self._collabs.pop(state.channel_id, None)
        state.channel_id = channel_id
        if channel_id:
            members = self._channels.setdefault(channel_id, [])
            if walker_id not in members:
                members.append(walker_id)
        return state

    def leave_channel(self, walker_id: str) -> WalkerChannelState:
        return self.set_channel(walker_id, None)

    def channel_members(self, channel_id: str) -> List[str]:
        return list(self._channels.get((channel_id or "").strip().lower(), []))

    def walker_channel_members(self, walker_id: str) -> List[str]:
        state = self.state_for(walker_id)
        if not state.channel_id:
            return []
        return self.channel_members(state.channel_id)

    def configure_collaboration(
        self,
        walker_id: str,
        *,
        enabled: Optional[bool] = None,
        role: Optional[str] = None,
        max_rounds: Optional[int] = None,
    ) -> WalkerChannelState:
        state = self.register_walker(walker_id)
        if enabled is not None:
            state.auto_collab = bool(enabled)
            if not state.auto_collab:
                self.set_collab_partner(walker_id, None)
                if state.channel_id:
                    self._collabs.pop(state.channel_id, None)
        if role is not None and role.strip():
            state.collab_role = role.strip()
        if max_rounds is not None:
            state.max_rounds = max(1, int(max_rounds))
        return state

    def set_collab_partner(self, walker_id: str, partner_id: Optional[str]) -> WalkerChannelState:
        state = self.register_walker(walker_id)
        new_p = (partner_id or "").strip() or None
        if new_p == walker_id:
            new_p = None
        old_p = state.collab_partner_id
        if old_p and old_p != new_p:
            buddy = self._walkers.get(old_p)
            if buddy and buddy.collab_partner_id == walker_id:
                buddy.collab_partner_id = None
        state.collab_partner_id = new_p
        if new_p:
            buddy = self.register_walker(new_p)
            if buddy.collab_partner_id and buddy.collab_partner_id != walker_id:
                prev = self._walkers.get(buddy.collab_partner_id)
                if prev and prev.collab_partner_id == new_p:
                    prev.collab_partner_id = None
            buddy.collab_partner_id = walker_id
        return state

    def start_collaboration(self, walker_id: str, goal: str) -> Optional[ActiveCollaboration]:
        state = self.state_for(walker_id)
        if not state.channel_id or not state.auto_collab:
            return None
        members = self.channel_members(state.channel_id)
        peer: Optional[str] = None
        if state.collab_partner_id and state.collab_partner_id in members and state.collab_partner_id != walker_id:
            peer = state.collab_partner_id
        collab = ActiveCollaboration(
            channel_id=state.channel_id,
            goal=goal.strip(),
            origin_walker_id=walker_id,
            last_speaker_id=None,
            round_index=0,
            max_rounds=max(1, state.max_rounds),
            peer_walker_id=peer,
        )
        self._collabs[state.channel_id] = collab
        return collab

    def active_collaboration_for_walker(self, walker_id: str) -> Optional[ActiveCollaboration]:
        state = self.state_for(walker_id)
        if not state.channel_id:
            return None
        collab = self._collabs.get(state.channel_id)
        if collab and collab.active:
            return collab
        return None

    def stop_collaboration(self, channel_id: Optional[str]) -> None:
        if not channel_id:
            return
        self._collabs.pop(channel_id.strip().lower(), None)

    def next_collaboration_target(self, channel_id: str, current_speaker_id: str) -> Optional[str]:
        collab = self._collabs.get(channel_id)
        if collab is None or not collab.active:
            return None
        if collab.round_index >= collab.max_rounds:
            self._collabs.pop(channel_id, None)
            return None
        members = self._channels.get(channel_id, [])
        if len(members) < 2:
            self._collabs.pop(channel_id, None)
            return None
        peer = collab.peer_walker_id
        if peer and peer in members and collab.origin_walker_id in members:
            if current_speaker_id == collab.origin_walker_id:
                target = peer
            elif current_speaker_id == peer:
                target = collab.origin_walker_id
            else:
                target = peer if current_speaker_id != peer else collab.origin_walker_id
        else:
            if current_speaker_id in members:
                idx = members.index(current_speaker_id)
                ordered = members[idx + 1 :] + members[: idx + 1]
            else:
                ordered = list(members)
            target = next((wid for wid in ordered if wid != current_speaker_id), None)
        if not target:
            self._collabs.pop(channel_id, None)
            return None
        collab.last_speaker_id = current_speaker_id
        collab.round_index += 1
        if collab.round_index >= collab.max_rounds:
            collab.active = False
        return target
