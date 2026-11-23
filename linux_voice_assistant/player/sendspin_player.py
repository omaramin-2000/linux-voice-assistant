"""Client for the control socket of a Sendspin player (``sendspin-cli``, e.g. the Music Assistant Local Audio add-on)."""

import json
import logging
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple, Union

CONTROL_SOCKET_NAME = "control.sock"
DUCK_MODE_DUCK = "duck"
DUCK_MODE_STOP = "stop"
DEFAULT_DUCK_STREAM_MATCH = "sendspin"

_LOGGER = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 1.0
_POLL_INTERVAL_SECONDS = 1.0
# While ducking is requested, poll faster so that a stream that appears in the meantime is ducked quickly
_DUCK_POLL_INTERVAL_SECONDS = 0.25
# (unreachable for at least this many seconds, poll interval in seconds), longest first
_UNREACHABLE_POLL_STEPS = ((600.0, 60.0), (120.0, 30.0), (30.0, 5.0))

_PACTL_TIMEOUT_SECONDS = 2.0
# Volume of a PulseAudio/PipeWire stream at 100 %
_PA_VOLUME_NORM = 65536
# Properties of a sink input that ``duck_stream_match`` is looked for in
_STREAM_MATCH_PROPERTIES = ("application.name", "application.process.binary", "media.name")


@dataclass(frozen=True)
class SendspinStatus:
    """
    What the Sendspin player reports.
    :param state: ``playing`` or ``idle`` (paused, stopped, unknown or no server connected, the protocol does not tell them apart).
    :param volume: Group volume 0-100, None if unknown.
    :param muted: Whether the group is muted.
    """

    state: str
    volume: Optional[int]
    muted: bool


@dataclass(frozen=True)
class _Stream:
    """A playback stream (sink input) of the local audio server."""

    index: int
    volumes: List[int]
    """Raw volume of every channel, ``_PA_VOLUME_NORM`` is 100 %."""


class SendspinPlayer:
    """
    Ducks, stops and resumes a Sendspin player through its control socket.

    The protocol is one request line per connection. The first reply line is ``ok`` or ``error <kind>: <reason>``,
    a ``status`` reply continues with ``key: value`` lines.

    Ducking never touches the Sendspin volume. The player has no per-endpoint duck, and ``vol`` sets the **group** volume that
    the entities show and that other players of the group follow. Instead, ``duck`` mode lowers the volume of the playback
    **stream** of the player on the local PulseAudio/PipeWire server (``pactl set-sink-input-volume``) to ``factor`` of its
    current value, exactly like halving ``mpv.volume`` does for the media player. Changing the Sendspin volume while ducked
    only changes ``vol``, so the stream stays ducked until :meth:`unduck`.

    In ``stop`` mode ducking stops the player instead and starts it again afterwards, the volume is not touched.
    All methods are thread-safe and never raise: if the player or ``pactl`` is unreachable, nothing is ducked.

    Needs ``pactl`` and access to the audio server that the player plays on (``PULSE_SERVER`` or the socket in ``XDG_RUNTIME_DIR``).
    """

    def __init__(
        self,
        socket_path: Union[str, Path],
        duck_mode: str = DUCK_MODE_STOP,
        duck_stream_match: str = DEFAULT_DUCK_STREAM_MATCH,
    ) -> None:
        """
        :param socket_path: Path of the control socket of the player (``--control-socket`` of ``sendspin-cli``).
        :param duck_mode: ``stop`` stops the player while ducked and starts it afterwards, ``duck`` lowers the volume of its audio stream.
        :param duck_stream_match: Text (case insensitive) that identifies the stream of the player among the sink inputs of the audio server.
            It is looked for in the application name, the binary and the media name, see ``pactl list sink-inputs``.
        """
        self._socket_path = str(socket_path)
        self._lock = threading.Lock()
        self._poll_thread: Optional[threading.Thread] = None
        self._reachable: Optional[bool] = None
        self._unreachable_since: Optional[float] = None
        self._on_duck_changed: Optional[Callable[[Optional[int], Optional[int]], None]] = None
        self._recover: Optional[Tuple[int, int]] = None
        self._duck_owners: Set[str] = set()
        self._duck_factor = 0.5
        self._duck_mode = duck_mode
        self._duck_stream_match = duck_stream_match.strip().lower()
        self._stopped_by_duck = False
        # Sink input index -> volume of its channels before ducking
        self._ducked_streams: Dict[int, List[int]] = {}
        self._stream_candidates: List[str] = []
        self._warned_no_stream = False
        self._warned_pactl = False

    def set_duck_persistence(self, on_duck_changed: Callable[[Optional[int], Optional[int]], None], saved: Tuple[Optional[int], Optional[int]]) -> None:
        """
        Remember the ducked stream volume across restarts. A volume saved by an earlier run is restored once the stream of the player shows up.
        :param on_duck_changed: Called with ``(stream volume before, stream volume after)`` in percent when ducking starts and with ``(None, None)`` when it ends.
        :param saved: ``(volume before, volume after)`` that an earlier run left behind, ``(None, None)`` if there is none.
        """
        self._on_duck_changed = on_duck_changed
        if saved[0] is not None and saved[1] is not None:
            self._recover = (saved[0], saved[1])

    @property
    def is_ducked(self) -> bool:
        """Whether the stream of the player is currently lowered or the player stopped by :meth:`duck`."""
        return bool(self._ducked_streams) or self._stopped_by_duck

    def start_polling(self, callback: Callable[[Optional[SendspinStatus]], None]) -> None:
        """
        Report the status of the player once a second from a daemon thread.
        :param callback: Called with the status, or None while the player is unreachable.
        """
        if self._poll_thread is not None:
            return

        def poll() -> None:
            last: Optional[SendspinStatus] = None
            while True:
                time.sleep(self._poll_interval())
                try:
                    self._recover_duck()
                    self._duck_if_requested()
                    status = self.status()
                    if status != last:
                        _LOGGER.debug("Sendspin status: %s", status)
                        last = status
                    callback(status)
                except Exception:  # pylint: disable=broad-exception-caught
                    _LOGGER.exception("Error in Sendspin status callback")

        self._poll_thread = threading.Thread(target=poll, daemon=True, name="sendspin-poll")
        self._poll_thread.start()

    def status(self) -> Optional[SendspinStatus]:
        """
        :return: The status of the player, None if it is unreachable.
        """
        with self._lock:
            raw = self._status()
            if raw is None:
                return None

            state = raw.get("state", "idle")
            if state != "playing":
                state = "idle"

            return SendspinStatus(state=state, volume=self._parse_volume(raw), muted="(muted)" in raw.get("group volume", ""))

    def play(self) -> None:
        """Start or resume playback."""
        self._send("play")

    def stop(self) -> None:
        """Stop playback."""
        self._send("stop")

    def pause_playback(self) -> None:
        """Pause playback on request of the user. Unlike :meth:`stop_for_announcement` it is not undone by :meth:`resume`."""
        self._send("pause")

    def set_volume(self, percent: int) -> None:
        """
        Set the group volume. Ducking works on the audio stream, so it is not affected and stays in place.
        :param percent: Volume 0-100.
        """
        self._send(f"vol {max(0, min(100, percent))}")

    def set_muted(self, muted: bool) -> None:
        """
        Mute or unmute the group.
        :param muted: True to mute.
        """
        self._send("mute on" if muted else "mute off")

    def duck(self, factor: float = 0.5, owner: str = "assistant") -> None:
        """
        Request ducking: lower the volume of the audio stream of the player to ``factor`` of its current value (or stop the player in ``stop`` mode).
        The request stays until :meth:`unduck`, so playback that starts later is ducked too.
        :param factor: Fraction of the current stream volume, 0.0 to 1.0, not used in ``stop`` mode.
        :param owner: Who asks. The volume is restored when every owner has called :meth:`unduck`.
        """
        with self._lock:
            self._duck_owners.add(owner)
            self._duck_factor = factor
            _LOGGER.debug("Sendspin duck requested by %s (owners: %s)", owner, sorted(self._duck_owners))
            self._apply_duck()

    def unduck(self, owner: str = "assistant") -> None:
        """
        End a ducking request. The stream volume saved by :meth:`duck` is restored (or the player started in ``stop`` mode) when no owner is left.
        :param owner: The owner that called :meth:`duck`.
        """
        with self._lock:
            self._duck_owners.discard(owner)
            _LOGGER.debug("Sendspin duck released by %s (owners left: %s, ducked: %s)", owner, sorted(self._duck_owners), self.is_ducked)
            if self._duck_owners:
                return

            if self._stopped_by_duck:
                self._stopped_by_duck = False
                _LOGGER.debug("Sendspin started again")
                self._send("play")
                return

            self._restore_streams()

    def is_playing(self) -> bool:
        """Whether the server reports the group as playing."""
        with self._lock:
            status = self._status()
            return (status is not None) and (status.get("state") == "playing")

    def _poll_interval(self) -> float:
        """Seconds to wait before the next poll: faster while ducking, the longer the socket is unreachable, the less often it is tried."""
        if self._duck_owners and self._duck_mode == DUCK_MODE_DUCK:
            return _DUCK_POLL_INTERVAL_SECONDS

        if self._unreachable_since is None:
            return _POLL_INTERVAL_SECONDS

        unreachable_for = time.monotonic() - self._unreachable_since
        for threshold, interval in _UNREACHABLE_POLL_STEPS:
            if unreachable_for >= threshold:
                return interval

        return _POLL_INTERVAL_SECONDS

    def _status(self) -> Optional[Dict[str, str]]:
        reply = self._request("status")
        if reply is None:
            return None

        status: Dict[str, str] = {}
        for line in reply[1:]:
            key, separator, value = line.partition(":")
            if separator:
                status[key.strip()] = value.strip()

        return status

    @staticmethod
    def _parse_volume(status: Optional[Dict[str, str]]) -> Optional[int]:
        if status is None:
            return None

        # The value reads `55` or `55 (muted)`
        words = status.get("group volume", "").split()
        return int(words[0]) if words and words[0].isdigit() else None

    @staticmethod
    def _percent(volumes: List[int]) -> int:
        """Volume of the first channel in percent."""
        return round(volumes[0] * 100 / _PA_VOLUME_NORM)

    def _ducked_level(self, volume: int) -> int:
        """The raw volume of a channel while ducked. A factor on the raw value is what halving ``mpv.volume`` does."""
        return max(0, int(volume * self._duck_factor))

    def _apply_duck(self) -> None:
        """Duck the player if ducking is requested. Expects the lock to be held."""
        if self._duck_mode == DUCK_MODE_STOP:
            if self._stopped_by_duck:
                return

            status = self._status()
            if (status is not None) and (status.get("state") == "playing") and self._send("stop"):
                _LOGGER.debug("Sendspin stopped for ducking (owners: %s)", sorted(self._duck_owners))
                self._stopped_by_duck = True
            return

        self._duck_streams()

    def _duck_streams(self) -> None:
        """Lower every stream of the player that is not ducked yet. Expects the lock to be held."""
        streams = self._list_streams()
        if streams is None:
            return

        present = {stream.index for stream in streams}
        for index in [index for index in self._ducked_streams if index not in present]:
            _LOGGER.debug("Sendspin stream %d is gone", index)
            del self._ducked_streams[index]

        if not streams and not self._ducked_streams and not self._warned_no_stream:
            self._warned_no_stream = True
            _LOGGER.warning(
                "No audio stream matching '%s' to duck Sendspin. Playing streams: %s. Set the match to part of the application name, binary or media name of the player",
                self._duck_stream_match,
                ", ".join(self._stream_candidates) or "none",
            )

        for stream in streams:
            if stream.index in self._ducked_streams:
                continue

            ducked = [self._ducked_level(volume) for volume in stream.volumes]
            if not self._set_stream_volume(stream.index, ducked):
                continue

            _LOGGER.debug(
                "Sendspin stream %d ducked from %d%% to %d%% (owners: %s)",
                stream.index,
                self._percent(stream.volumes),
                self._percent(ducked),
                sorted(self._duck_owners),
            )
            first = not self._ducked_streams
            self._ducked_streams[stream.index] = stream.volumes
            if first:
                self._notify_duck(self._percent(stream.volumes), self._percent(ducked))

    def _restore_streams(self) -> None:
        """Give the ducked streams their volume back. Expects the lock to be held."""
        if not self._ducked_streams:
            return

        saved, self._ducked_streams = self._ducked_streams, {}
        streams = self._list_streams()
        present = None if streams is None else {stream.index for stream in streams}
        for index, volumes in saved.items():
            if present is not None and index not in present:
                continue

            if self._set_stream_volume(index, volumes):
                _LOGGER.debug("Sendspin stream %d restored to %d%%", index, self._percent(volumes))
            else:
                _LOGGER.warning("Could not restore the volume of Sendspin stream %d to %d%%", index, self._percent(volumes))

        self._notify_duck(None, None)

    def _duck_if_requested(self) -> None:
        """Duck playback that started while ducking was requested."""
        with self._lock:
            if self._duck_owners:
                self._apply_duck()

    def _notify_duck(self, volume_from: Optional[int], volume_to: Optional[int]) -> None:
        if self._on_duck_changed is not None:
            self._on_duck_changed(volume_from, volume_to)

    def _recover_duck(self) -> None:
        """Restore the stream volume of a ducking that an earlier run did not finish, once the stream of the player shows up."""
        with self._lock:
            if self._recover is None:
                return

            streams = self._list_streams()
            if not streams:
                return

            volume_from, volume_to = self._recover
            self._recover = None
            for stream in streams:
                percent = self._percent(stream.volumes)
                if stream.index in self._ducked_streams or percent == 0:
                    continue

                if abs(percent - volume_to) <= 1 and percent != volume_from:
                    _LOGGER.warning("Restoring Sendspin stream %d to %d%%, an earlier run left it ducked at %d%%", stream.index, volume_from, percent)
                    self._set_stream_volume(stream.index, [int(volume * volume_from / percent) for volume in stream.volumes])
            self._notify_duck(None, None)

    def _list_streams(self) -> Optional[List[_Stream]]:
        """
        :return: The streams of the player among the sink inputs of the audio server, None if the audio server cannot be asked.
        """
        output = self._pactl("--format=json", "list", "sink-inputs")
        if output is None:
            return None

        try:
            entries = json.loads(output)
        except ValueError:
            _LOGGER.debug("Cannot parse the list of sink inputs", exc_info=True)
            return None

        streams: List[_Stream] = []
        candidates: List[str] = []
        for entry in entries:
            properties = entry.get("properties") or {}
            texts = [str(properties.get(name, "")) for name in _STREAM_MATCH_PROPERTIES]
            candidates.append(" / ".join(text for text in texts if text) or f"#{entry.get('index')}")
            if self._duck_stream_match not in " ".join(texts).lower():
                continue

            volumes = [int(channel["value"]) for channel in (entry.get("volume") or {}).values()]
            if volumes:
                streams.append(_Stream(index=int(entry["index"]), volumes=volumes))

        self._stream_candidates = candidates
        if streams:
            self._warned_no_stream = False

        return streams

    def _set_stream_volume(self, index: int, volumes: List[int]) -> bool:
        return self._pactl("set-sink-input-volume", str(index), *[str(volume) for volume in volumes]) is not None

    def _pactl(self, *arguments: str) -> Optional[str]:
        """
        :return: The output of ``pactl``, None if it cannot be run or fails.
        """
        try:
            result = subprocess.run(["pactl", *arguments], capture_output=True, text=True, timeout=_PACTL_TIMEOUT_SECONDS, check=False)
        except FileNotFoundError:
            if not self._warned_pactl:
                self._warned_pactl = True
                _LOGGER.critical("pactl is not installed (package pulseaudio-utils), Sendspin cannot be ducked")
            return None
        except (OSError, subprocess.SubprocessError) as err:
            _LOGGER.debug("pactl %s failed: %s", " ".join(arguments), err)
            return None

        if result.returncode != 0:
            _LOGGER.debug("pactl %s failed: %s", " ".join(arguments), result.stderr.strip())
            return None

        return result.stdout

    def _send(self, command: str) -> bool:
        return self._request(command) is not None

    def _request(self, command: str) -> Optional[list]:
        """
        :return: The reply lines after the leading ``ok``, or None if the player is unreachable or refused the command.
        """
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(_TIMEOUT_SECONDS)
                sock.connect(self._socket_path)
                sock.sendall(f"{command}\n".encode("utf-8"))

                chunks = []
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
        except OSError as err:
            if self._reachable is not False:
                _LOGGER.critical(
                    "Sendspin control socket %s is not reachable (%s). Check that the player runs, that the socket is mounted and that it belongs to the user of LVA",
                    self._socket_path,
                    err,
                )
            else:
                _LOGGER.debug("Sendspin control socket %s is still not reachable (%s)", self._socket_path, err)
            self._reachable = False
            if self._unreachable_since is None:
                self._unreachable_since = time.monotonic()
            return None

        if not self._reachable:
            _LOGGER.info("Sendspin control socket %s is reachable", self._socket_path)
        self._reachable = True
        self._unreachable_since = None

        lines = b"".join(chunks).decode("utf-8", errors="replace").splitlines()
        if command != "status":
            _LOGGER.debug("Sendspin command '%s' -> %s", command, lines[0] if lines else "no reply")
        if not lines or lines[0] != "ok":
            _LOGGER.debug("Sendspin player refused '%s': %s", command, lines[0] if lines else "no reply")
            return None

        return lines
