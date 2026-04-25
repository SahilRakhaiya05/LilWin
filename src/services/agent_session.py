
from PyQt6.QtCore import QObject, pyqtSignal

class AgentSession(QObject):
    # Signals for UI updates
    text_received = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    tool_used = pyqtSignal(str)
    turn_completed = pyqtSignal()
    busy_state_changed = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.is_running = False
        self.is_busy = False
        self.history = []

    def start(self):
        raise NotImplementedError()

    def send(self, message: str):
        raise NotImplementedError()

    def terminate(self):
        raise NotImplementedError()

    def restart(self) -> None:
        """Stop any underlying process / connection and bring the session back up.

        Default implementation is a terminate + start cycle, which is correct
        for subclasses that hold a long-lived handle (Claude stream-json,
        OpenClaw WebSocket). One-shot CLIs (Gemini / Codex / Copilot) override
        this with a history-reset instead.
        """
        try:
            self.terminate()
        except Exception:  # pragma: no cover - defensive
            pass
        self.is_running = False
        self.is_busy = False
        self.start()

    def clear_history(self):
        self.history = []
