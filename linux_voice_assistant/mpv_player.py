"""Media player using mpv in a subprocess."""

import logging
from collections.abc import Callable
from threading import Lock
from typing import List, Optional, Union

from mpv import MPV

_LOGGER = logging.getLogger(__name__)


class MpvMediaPlayer:
    def __init__(self, device: Optional[str] = None) -> None:
        # main python-mpv player instance
        self.player = MPV()

        if device:
            try:
                # setting audio-device may fail on some builds, guard it
                self.player["audio-device"] = device
            except Exception:
                _LOGGER.debug("Failed to set audio-device on python-mpv instance", exc_info=True)

        # public flag used by older code paths
        self.is_playing = False

        # internal playlist + done-callback handling
        self._playlist: List[str] = []
        self._done_callback: Optional[Callable[[], None]] = None
        self._done_callback_lock = Lock()

        # state lock for small, frequent checks (used by is_idle())
        self._state_lock = Lock()

        # placeholder for a tracked subprocess (if you later add subprocess playback)
        self._current_proc: Optional[object] = None

        # ducking values
        self._duck_ratio: float = 0.2
        self._unduck_volume: int = 100
        self._duck_volume: int = self._compute_duck_volume()

        # attach end-file callback if supported; fail silently if not
        try:
            self.player.event_callback("end-file")(self._on_end_file)
        except Exception:
            _LOGGER.debug("python-mpv event_callback('end-file') not available or failed to attach", exc_info=True)

    def play(
        self,
        url: Union[str, List[str]],
        done_callback: Optional[Callable[[], None]] = None,
        stop_first: bool = True,
    ) -> None:
        self.stop()

        if isinstance(url, str):
            self._playlist = [url]
        else:
            # copy to avoid external list mutation
            self._playlist = list(url)

        next_url = self._playlist.pop(0)
        _LOGGER.debug("Playing %s", next_url)

        self._done_callback = done_callback
        self.is_playing = True
        try:
            # prefer 'loadfile' / play semantics offered by python-mpv API
            # some builds expose .play(); using .play() is fine as before
            self.player.play(next_url)
        except Exception:
            _LOGGER.exception("Error calling python-mpv.play for %s", next_url)

    def pause(self) -> None:
        try:
            self.player.pause = True
        except Exception:
            _LOGGER.exception("Failed to pause player")
        # note: we still set our local flag; is_idle() will use better checks when available
        self.is_playing = False

    def resume(self) -> None:
        try:
            self.player.pause = False
        except Exception:
            _LOGGER.exception("Failed to resume player")
        # if playlist remains or we resumed, mark as playing
        if self._playlist:
            self.is_playing = True
        else:
            # conservatively assume resume succeeded
            self.is_playing = True

    def stop(self) -> None:
        try:
            # try command API first, then fallback to method if present
            try:
                self.player.command("stop")
            except Exception:
                # some python-mpv versions provide a stop() method
                try:
                    self.player.stop()
                except Exception:
                    _LOGGER.exception("Failed to stop python-mpv player")
        except Exception:
            _LOGGER.exception("Error while stopping player")
        self._playlist.clear()

    def duck(self) -> None:
        try:
            self.player.volume = self._duck_volume
        except Exception:
            _LOGGER.exception("Failed to duck volume")

    def unduck(self) -> None:
        try:
            self.player.volume = self._unduck_volume
        except Exception:
            _LOGGER.exception("Failed to unduck volume")

    def set_volume(self, volume: int) -> None:
        volume = max(0, min(100, volume))
        try:
            self.player.volume = volume
        except Exception:
            _LOGGER.exception("Failed to set volume to %s", volume)

        # update stored volumes and recompute duck level once
        self._unduck_volume = volume
        self._duck_volume = self._compute_duck_volume()

    def _compute_duck_volume(self) -> int:
        if self._unduck_volume <= 0:
            return 0

        return max(1, int(round(self._unduck_volume * self._duck_ratio)))

    def _on_end_file(self, event) -> None:
        if self._playlist:
            try:
                next_item = self._playlist.pop(0)
                self.player.play(next_item)
                return
            except Exception:
                _LOGGER.exception("Failed to play next item in playlist via python-mpv")

        self.is_playing = False

        todo_callback: Optional[Callable[[], None]] = None
        with self._done_callback_lock:
            if self._done_callback:
                todo_callback = self._done_callback
                self._done_callback = None

        if todo_callback:
            try:
                todo_callback()
            except Exception:
                _LOGGER.exception("Unexpected error running done callback")

    # -----------------
    # New: robust idle/play state checks
    # -----------------
    def is_idle(self) -> bool:
        """Thread-safe check whether the underlying player is idle.

        Order of checks:
        1. if there is a tracked subprocess (_current_proc), consider NOT idle.
        2. try python-mpv property get_property('idle-active') (preferred).
        3. fallback to python-mpv dict-style access player['idle-active'].
        4. fallback to local is_playing flag (may be stale).
        """
        with self._state_lock:
            # if we have an active tracked subprocess, it's playing
            if getattr(self, "_current_proc", None):
                return False

            player = getattr(self, "player", None)
            if player is not None:
                try:
                    # try get_property first (some python-mpv versions)
                    idle_prop = None
                    try:
                        idle_prop = player.get_property("idle-active")
                    except Exception:
                        # ignore and try dict-style below
                        idle_prop = None

                    if idle_prop is None:
                        try:
                            idle_prop = player["idle-active"]
                        except Exception:
                            idle_prop = None

                    if idle_prop is not None:
                        # idle-active True means mpv is idle (not playing)
                        return bool(idle_prop)
                except Exception:
                    _LOGGER.debug("Error querying python-mpv idle-active property", exc_info=True)

            # final fallback: use local flag
            return not bool(getattr(self, "is_playing", False))

    def is_currently_playing(self) -> bool:
        """Convenience accessor (thread-safe-ish): True if something is playing."""
        return not self.is_idle()
