
import logging
import os
import random
from typing import List, Optional

from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtCore import QUrl

logger = logging.getLogger(__name__)


class SoundManager:
    """Plays `ping-*.mp3` on turn completion.

    If ``variant`` is set to a letter group (e.g. ``"aa"``) and
    ``Sounds/ping-aa.mp3`` exists, that file is preferred. Otherwise we pick a
    random ping-*.mp3 from the sounds directory. Missing files or missing
    QtMultimedia backend are handled quietly.
    """

    def __init__(self) -> None:
        self._available = True
        try:
            self.player = QMediaPlayer()
            self.audio_output = QAudioOutput()
            self.player.setAudioOutput(self.audio_output)
            self.audio_output.setVolume(0.5)
        except Exception:
            logger.debug("QMediaPlayer unavailable; sound disabled", exc_info=True)
            self._available = False
            self.player = None
            self.audio_output = None

        assets_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
        self.sounds_dir = os.path.join(assets_dir, "Sounds")
        self.ping_sounds: List[str] = []
        if os.path.isdir(self.sounds_dir):
            self.ping_sounds = sorted(
                f for f in os.listdir(self.sounds_dir) if f.startswith("ping-") and f.lower().endswith(".mp3")
            )
        self._variant: Optional[str] = None

    def set_variant(self, variant: Optional[str]) -> None:
        self._variant = (variant or "").strip().lower() or None

    def _preferred_file(self) -> Optional[str]:
        if not self.ping_sounds:
            return None
        if self._variant:
            exact = f"ping-{self._variant}.mp3"
            if exact in self.ping_sounds:
                return exact
        return random.choice(self.ping_sounds)

    def play_done(self) -> None:
        if not self._available or not self.player:
            return
        name = self._preferred_file()
        if not name:
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.stop()
        sound_path = os.path.join(self.sounds_dir, name)
        try:
            self.player.setSource(QUrl.fromLocalFile(sound_path))
            self.player.play()
        except Exception:
            logger.debug("Failed to play %s", sound_path, exc_info=True)


if __name__ == "__main__":
    # Test sound manager
    from PyQt6.QtWidgets import QApplication
    import sys
    app = QApplication(sys.argv)
    sm = SoundManager()
    sm.play_done()
    sys.exit(app.exec())
