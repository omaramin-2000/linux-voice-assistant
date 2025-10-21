"""Media player using mpv (python-mpv for main player) with subprocess TTS.

This version keeps the original ctor signature (device: Optional[str] = None)
so it is compatible with your existing code. It defaults to Pulse/pipewire
for subprocesses and the python-mpv instance to avoid ALSA exclusive-locks.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
import os
from collections.abc import Callable
from threading import RLock
from typing import List, Optional, Union

try:
    # python-mpv is optional; we use it for main playback when available
    from mpv import MPV  # type: ignore
    _HAVE_PYTHON_MPV = True
except Exception:
    MPV = None  # type: ignore
    _HAVE_PYTHON_MPV = False

_LOGGER = logging.getLogger(__name__)


class MpvMediaPlayer:
    def __init__(self, device: Optional[str] = None) -> None:
        """
        device: mpv audio-device string (e.g. 'pulse' or a specific sink). If None,
                this class will default to 'pulse' which avoids ALSA exclusive-locks.
        NOTE: Constructor signature kept backward-compatible with your code.
        """
        # choose pulse by default to avoid ALSA device-exclusive blocking on many systems
        self._audio_device = device or "pulse"
        self._client_name: Optional[str] = None  # set externally if you want distinctions

        # default mpv binary name used for subprocess playback
        self._mpv_binary = "mpv"

        # per-instance thread-safety lock
        self._lock = RLock()

        # tracked subprocess (if any) so we can interrupt announcements
        self._current_proc: Optional[subprocess.Popen] = None

        # try to initialize python-mpv main player in idle mode (keeps device warmed)
        self.player = None
        if _HAVE_PYTHON_MPV:
            try:
                # idle=True keeps the device open and can reduce first-play latency
                self.player = MPV(idle=True, no_video=True)
                # set audio device if possible
                try:
                    self.player["audio-device"] = self._audio_device
                except Exception:
                    _LOGGER.debug("Failed to set audio-device on python-mpv; proceeding", exc_info=True)
                _LOGGER.debug("python-mpv main player initialized (idle=True)")
            except Exception:
                _LOGGER.exception("Failed to initialize python-mpv instance; falling back to subprocess-only mode")
                self.player = None

        # public flag used by other code (keeps compatibility)
        self.is_playing = False

        # basic playlist & done-callback support (kept for compatibility)
        self._playlist: List[str] = []
        self._done_callback: Optional[Callable[[], None]] = None
        self._done_callback_lock = threading.Lock()

        # ducking values
        self._duck_ratio: float = 0.2
        self._unduck_volume: int = 100
        self._duck_volume: int = self._compute_duck_volume()

        # Try registering end-file callback if python-mpv available
        if getattr(self, "player", None) is not None:
            try:
                self.player.event_callback("end-file")(self._on_end_file)
            except Exception:
                _LOGGER.debug("python-mpv end-file callback registration failed (continuing)", exc_info=True)

    # -----------------
    # Public API (keeps your original signatures)
    # -----------------
    def play(
        self,
        url: Union[str, List[str]],
        done_callback: Optional[Callable[[], None]] = None,
        stop_first: bool = True,
    ) -> None:
        # keep original behavior: stop current, queue, and play first
        if stop_first:
            self.stop()

        if isinstance(url, str):
            self._playlist = [url]
        else:
            self._playlist = list(url)

        if not self._playlist:
            return

        next_url = self._playlist.pop(0)
        _LOGGER.debug("Playing %s", next_url)

        with self._done_callback_lock:
            self._done_callback = done_callback

        # Prefer python-mpv for local files if available (gives better control)
        if not self._is_network_url(next_url) and getattr(self, "player", None) is not None:
            try:
                self.player.command("loadfile", next_url, "replace")
                self.is_playing = True
                return
            except Exception:
                _LOGGER.exception("python-mpv failed to play %s; falling back to subprocess", next_url)

        # For network URLs (and fallback), play in a background subprocess (gives caching + no device contention)
        self.is_playing = True
        t = threading.Thread(target=self._play_subprocess_playlist, args=([next_url] + self._playlist,), daemon=True)
        # playlist ownership moves to worker
        self._playlist.clear()
        t.start()

    def pause(self) -> None:
        # Pause python-mpv if available (keeps state consistent)
        try:
            if getattr(self, "player", None) is not None:
                self.player.pause = True
        except Exception:
            _LOGGER.exception("Failed to pause python-mpv")

        # Note: for subprocess-based playback, we do NOT use SIGSTOP by default because
        # SIGSTOP can leave audio sinks in strange states on some systems.
        # Announcements are played via separate subprocesses so they will usually mix in.
        with self._lock:
            self.is_playing = False

    def resume(self) -> None:
        try:
            if getattr(self, "player", None) is not None:
                self.player.pause = False
        except Exception:
            _LOGGER.exception("Failed to resume python-mpv")
        with self._lock:
            # If playlist is non-empty or we resumed, mark as playing
            self.is_playing = True

    def stop(self) -> None:
        try:
            if getattr(self, "player", None) is not None:
                try:
                    self.player.command("stop")
                except Exception:
                    try:
                        self.player.stop()
                    except Exception:
                        _LOGGER.exception("Failed to stop python-mpv player")
        except Exception:
            _LOGGER.exception("Error while stopping player")

        # terminate tracked subprocess if present (best-effort)
        with self._lock:
            if self._current_proc:
                try:
                    self._current_proc.terminate()
                except Exception:
                    _LOGGER.exception("Failed to terminate subprocess on stop")
                finally:
                    try:
                        self._current_proc.wait(timeout=0.5)
                    except Exception:
                        pass
                    self._current_proc = None

            self._playlist.clear()
            self.is_playing = False

    def duck(self) -> None:
        try:
            if getattr(self, "player", None) is not None:
                self.player.volume = self._duck_volume
        except Exception:
            _LOGGER.exception("Failed to duck volume")

    def unduck(self) -> None:
        try:
            if getattr(self, "player", None) is not None:
                self.player.volume = self._unduck_volume
        except Exception:
            _LOGGER.exception("Failed to unduck volume")

    def set_volume(self, volume: int) -> None:
        volume = max(0, min(100, int(volume)))
        try:
            if getattr(self, "player", None) is not None:
                self.player.volume = volume
        except Exception:
            _LOGGER.exception("Failed to set volume to %s", volume)

        with self._lock:
            self._unduck_volume = volume
            self._duck_volume = self._compute_duck_volume()

    def _compute_duck_volume(self) -> int:
        if self._unduck_volume <= 0:
            return 0
        return max(1, int(round(self._unduck_volume * self._duck_ratio)))

    # -----------------
    # Internal continuation when python-mpv ends a file
    # -----------------
    def _on_end_file(self, event) -> None:
        with self._lock:
            if self._playlist:
                next_item = self._playlist.pop(0)
                # prefer python-mpv for local items, else hand off to subprocess
                try:
                    if self._is_network_url(next_item) or getattr(self, "player", None) is None:
                        t = threading.Thread(target=self._play_subprocess_playlist, args=([next_item] + self._playlist,), daemon=True)
                        self._playlist.clear()
                        t.start()
                        return
                    else:
                        self.player.command("loadfile", next_item, "replace")
                        return
                except Exception:
                    _LOGGER.exception("Failed to continue playlist via python-mpv; falling back to subprocess")
                    t = threading.Thread(target=self._play_subprocess_playlist, args=([next_item] + self._playlist,), daemon=True)
                    self._playlist.clear()
                    t.start()
                    return

            # no more items
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
    # TTS / subprocess playback (tracked so it can be interrupted)
    # -----------------
    def speak_tts_blocking(self, tts_path: str, extra_args: Optional[List[str]] = None) -> int:
        """Play TTS (blocking) via a separate mpv subprocess so it cannot alter python-mpv state."""
        cmd = self._build_subprocess_cmd(extra_args or [], tts_path)

        if shutil.which(self._mpv_binary) is None:
            _LOGGER.warning("mpv binary '%s' not found on PATH; cannot play TTS", self._mpv_binary)
            return -1

        proc = None
        try:
            with self._lock:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._current_proc = proc
                self.is_playing = True
            ret = proc.wait()
            return ret
        except Exception:
            _LOGGER.exception("Error running mpv TTS subprocess")
            return -1
        finally:
            with self._lock:
                if self._current_proc is proc:
                    self._current_proc = None
                self.is_playing = False

    def speak_tts_async(self, tts_path: str, extra_args: Optional[List[str]] = None) -> threading.Thread:
        def _worker():
            try:
                self.speak_tts_blocking(tts_path, extra_args=extra_args)
            except Exception:
                _LOGGER.exception("Exception in TTS worker thread")

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        return t

    def _play_subprocess_playlist(self, playlist: List[str]) -> None:
        """Play a list of items via subprocess (blocking inside this thread)."""
        if not playlist:
            return

        if shutil.which(self._mpv_binary) is None:
            _LOGGER.warning("mpv binary '%s' not found on PATH; cannot play subprocess playlist", self._mpv_binary)
            return

        cmd = self._build_subprocess_cmd(None, *playlist)
        _LOGGER.debug("Starting subprocess mpv playlist: %s", cmd)

        proc = None
        try:
            with self._lock:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self._current_proc = proc
                self.is_playing = True

            proc.wait()
            _LOGGER.debug("Subprocess mpv finished with returncode=%s", getattr(proc, "returncode", None))
        except Exception:
            _LOGGER.exception("Error running subprocess mpv playlist")
        finally:
            with self._lock:
                if self._current_proc is proc:
                    self._current_proc = None
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
                    _LOGGER.exception("Unexpected error running done callback after subprocess playlist")

    # -----------------
    # Helpers
    # -----------------
    def _build_subprocess_cmd(self, extra_args: Optional[List[str]], *items: str) -> List[str]:
        """Construct a subprocess mpv command with audio-device and client name flags."""
        cmd: List[str] = [self._mpv_binary]

        # append stream args (we keep them minimal; you can extend if desired)
        # using a cache for network streams is helpful on Pi, but we avoid forcing a specific cache here
        cmd += ["--no-video", "--really-quiet"]

        if extra_args:
            cmd += list(extra_args)

        # audio device (pulse default avoids ALSA exclusive locks)
        if self._audio_device:
            cmd.append(f"--audio-device={self._audio_device}")

        # optional client name (useful to identify sinks in pavucontrol)
        if self._client_name:
            cmd.append(f"--audio-client-name={self._client_name}")

        # append playlists / urls
        cmd += list(items)
        return cmd

    def _is_network_url(self, url: str) -> bool:
        return isinstance(url, str) and url.lower().startswith(("http://", "https://"))

    # -----------------
    # State inspection helpers (thread-safe-ish)
    # -----------------
    def is_idle(self) -> bool:
        """Return True if nothing is currently producing audio from this player."""
        with self._lock:
            # if a tracked subprocess exists, not idle
            if getattr(self, "_current_proc", None):
                return False

            # try python-mpv property if available
            player = getattr(self, "player", None)
            if player is not None:
                try:
                    try:
                        idle_prop = player.get_property("idle-active")
                    except Exception:
                        idle_prop = None

                    if idle_prop is None:
                        try:
                            idle_prop = player["idle-active"]
                        except Exception:
                            idle_prop = None

                    if idle_prop is not None:
                        return bool(idle_prop)
                except Exception:
                    _LOGGER.debug("Error reading python-mpv idle-active", exc_info=True)

            # fallback to internal flag
            return not bool(getattr(self, "is_playing", False))

    def is_currently_playing(self) -> bool:
        """Convenience."""

        return
