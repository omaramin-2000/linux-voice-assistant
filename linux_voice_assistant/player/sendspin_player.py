"""Sendspin-aware music player using MPV + FIFO for audio output."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from time import time
from typing import Callable, List, Optional, Union

from aiosendspin.client import ClientListener, SendspinClient
from aiosendspin.models.core import DeviceInfo, StreamStartMessage
from aiosendspin.models.player import ClientHelloPlayerSupport, SupportedAudioFormat
from aiosendspin.models.types import AudioCodec, PlayerCommand, Roles
from aiosendspin.noise.keys import Identity
from aiosendspin.noise.trust_store import ClientPairingRecord, ClientPairingStore

from .libmpv import LibMpvPlayer
from .state import PlayerState

_LOGGER = logging.getLogger(__name__)

@dataclass
class _PairingConfig:
    """In-memory pairing configuration."""
    static_pin: Optional[str] = None
    pairing_psk: Optional[bytes] = None
    pin_failure_count: int = 0


class FilePairingStore(ClientPairingStore):
    """
    Persistent pairing store backed by a JSON file.
    """

    def __init__(self, file_path: Path) -> None:
        self._file = file_path
        self._records: dict[str, ClientPairingRecord] = {}
        self._config: _PairingConfig = _PairingConfig()
        self._load()

    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            data = json.loads(self._file.read_text())
            self._config.static_pin = data.get("static_pin")
            self._config.pairing_psk = (
                data.get("pairing_psk").encode() if data.get("pairing_psk") else None
            )
            self._config.pin_failure_count = data.get("pin_failure_count", 0)
            for rec_data in data.get("records", []):
                rec = ClientPairingRecord(**rec_data)
                key = rec.server_id or rec.psk_id
                if key:
                    self._records[key] = rec
        except Exception:
            _LOGGER.debug("Failed to load pairing store", exc_info=True)

    def _save(self) -> None:
        try:
            data = {
                "static_pin": self._config.static_pin,
                "pairing_psk": (
                    self._config.pairing_psk.decode()
                    if self._config.pairing_psk
                    else None
                ),
                "pin_failure_count": self._config.pin_failure_count,
                "records": [
                    {
                        "server_id": r.server_id,
                        "psk_id": r.psk_id,
                        "psk": r.psk.decode() if r.psk else None,
                        "server_name": r.server_name,
                        "server_url": r.server_url,
                        "created_at": r.created_at,
                        "last_used": r.last_used,
                    }
                    for r in self._records.values()
                ],
            }
            self._file.write_text(json.dumps(data, indent=2))
        except Exception:
            _LOGGER.debug("Failed to save pairing store", exc_info=True)

    # --- Pairing config ---

    def get_pairing_config(self) -> dict:
        return {
            "static_pin": self._config.static_pin,
            "pairing_psk": self._config.pairing_psk,
            "pin_failure_count": self._config.pin_failure_count,
        }

    def store_pairing_config(self, config: dict) -> None:
        self._config.static_pin = config.get("static_pin")
        self._config.pairing_psk = config.get("pairing_psk")
        self._config.pin_failure_count = config.get("pin_failure_count", 0)
        self._save()

    def static_pin(self) -> Optional[str]:
        return self._config.static_pin

    def set_static_pin(self, pin: Optional[str]) -> None:
        self._config.static_pin = pin
        self._save()

    def clear_static_pin(self) -> None:
        self._config.static_pin = None
        self._save()

    def pairing_psk(self) -> Optional[bytes]:
        return self._config.pairing_psk

    def set_pairing_psk(self, psk: Optional[bytes]) -> None:
        self._config.pairing_psk = psk
        self._save()

    def clear_pairing_psk(self) -> None:
        self._config.pairing_psk = None
        self._save()

    def pin_failure_count(self) -> int:
        return self._config.pin_failure_count

    def record_pin_failure(self) -> None:
        self._config.pin_failure_count += 1
        self._save()

    def reset_pin_failures(self) -> None:
        self._config.pin_failure_count = 0
        self._save()

    def is_pin_locked_out(self) -> bool:
        return self._config.pin_failure_count >= 5

    # --- Records ---

    def list_records(self) -> List[ClientPairingRecord]:
        return list(self._records.values())

    def store_record(self, record: ClientPairingRecord) -> None:
        key = record.server_id or record.psk_id
        if key:
            self._records[key] = record
            self._save()

    def remove_record(self, record: ClientPairingRecord) -> None:
        for k, v in list(self._records.items()):
            if v is record:
                del self._records[k]
                self._save()
                break

    def record_by_server_id(self, server_id: str) -> Optional[ClientPairingRecord]:
        return self._records.get(server_id)

    def record_by_psk_id(self, psk_id: str) -> Optional[ClientPairingRecord]:
        for rec in self._records.values():
            if rec.psk_id == psk_id:
                return rec
        return None

    def resolve_by_psk_id(self, psk_id: str) -> Optional[ClientPairingRecord]:
        return self.record_by_psk_id(psk_id)

    def mark_record_used(self, record: ClientPairingRecord) -> None:
        record.last_used = time.time()
        self._save()

class SendspinMediaPlayer:
    """
    Drop-in replacement for MpvMediaPlayer as music_player.
    Uses ClientListener (daemon-style) so MA discovers LVA via mDNS.
    TTS/announcements remain on tts_player.
    """

    def __init__(
        self,
        device: str | None = None,
        fifo_path: str | None = None,
        client_id: str | None = None,
        client_name: str = "LVA Speaker",
        listen_port: int = 8928,
        pairing_file: str | None = None,
    ) -> None:
        self._log = logging.getLogger(self.__class__.__name__)

        self._client_id = client_id or "lva-unknown"
        self._client_name = client_name
        self._listen_port = listen_port
        self._pairing_file = Path(
            pairing_file or f"/tmp/lva_sendspin_pairing_{self._client_id}.json"
        )

        # Internal MPV configured for rawaudio FIFO input
        self._player = LibMpvPlayer(device=device, rawaudio=True)

        self._fifo_path = Path(
            fifo_path or f"/tmp/lva_sendspin_fifo_{os.getpid()}"
        )
        self._fifo_fd: Optional[int] = None
        self._setup_fifo()

        self._client: Optional[SendspinClient] = None
        self._listener: Optional[ClientListener] = None
        self._stream_active = False
        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # FIFO
    # ------------------------------------------------------------------

    def _setup_fifo(self) -> None:
        if self._fifo_path.exists():
            self._fifo_path.unlink()
        os.mkfifo(self._fifo_path)
        self._fifo_fd = os.open(self._fifo_path, os.O_RDWR)
        self._log.debug("FIFO ready at %s", self._fifo_path)

    def _cleanup_fifo(self) -> None:
        if self._fifo_fd is not None:
            os.close(self._fifo_fd)
            self._fifo_fd = None

    def _reset_fifo(self) -> None:
        self._cleanup_fifo()
        if self._fifo_path.exists():
            self._fifo_path.unlink()
        self._setup_fifo()

    # ------------------------------------------------------------------
    # Sendspin lifecycle
    # ------------------------------------------------------------------

    async def start_sendspin(
        self,
        server_url: str | None = None,
        client_name: str | None = None,
    ) -> None:
        """Start Sendspin. If no URL, listen for MA (daemon mode)."""
        self._running = True
        name = client_name or self._client_name

        if server_url:
            self._task = asyncio.create_task(self._client_loop(server_url, name))
        else:
            self._task = asyncio.create_task(self._listener_loop(name))

    async def stop_sendspin(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._client:
            await self._client.disconnect()
        if self._listener:
            await self._listener.stop()
        self._cleanup_fifo()
        if self._fifo_path.exists():
            self._fifo_path.unlink()

    # ------------------------------------------------------------------
    # Server-initiated mode (daemon-style, mDNS)
    # ------------------------------------------------------------------

    async def _listener_loop(self, client_name: str) -> None:
        """Listen for incoming Music Assistant connections."""
        _LOGGER.info(
            "Starting Sendspin listener on port %d (mDNS: _sendspin._tcp.local.)",
            self._listen_port,
        )

        self._listener = ClientListener(
            client_id=self._client_id,
            on_connection=self._handle_server_connection,
            port=self._listen_port,
            path="/sendspin",
            host="0.0.0.0",
            advertise_mdns=True,
            client_name=client_name,
        )
        await self._listener.start()
        _LOGGER.info("Sendspin listener started. Waiting for Music Assistant...")

        while self._running:
            await asyncio.sleep(1.0)

    async def _handle_server_connection(self, ws) -> None:
        """Handle incoming connection from Music Assistant."""
        _LOGGER.info("Music Assistant connected to Sendspin listener")

        client = self._create_client()
        self._attach_callbacks(client)

        try:
            await client.attach_websocket(ws)
            self._client = client
            _LOGGER.info("Sendspin handshake complete with Music Assistant")

            # Wait for disconnect
            disconnect_event = asyncio.Event()
            unsub = client.add_disconnect_listener(disconnect_event.set)
            await disconnect_event.wait()
            unsub()
            _LOGGER.info("Music Assistant disconnected")
        except Exception:
            _LOGGER.exception("Error in Sendspin server connection")
        finally:
            if self._client is client:
                self._client = None
            self._stream_active = False
            self._cleanup_fifo()

    # ------------------------------------------------------------------
    # Client-initiated mode (direct URL)
    # ------------------------------------------------------------------

    async def _client_loop(self, url: str, client_name: str) -> None:
        """Connect to Music Assistant server with auto-reconnect."""
        backoff = 1.0
        while self._running:
            client = self._create_client()
            self._attach_callbacks(client)
            try:
                _LOGGER.info("Connecting to Sendspin server at %s...", url)
                await client.connect(url)
                self._client = client
                _LOGGER.info("Sendspin connected to %s", url)
                backoff = 1.0

                disconnect_event = asyncio.Event()
                unsub = client.add_disconnect_listener(disconnect_event.set)
                await disconnect_event.wait()
                unsub()
                _LOGGER.info("Sendspin disconnected, reconnecting...")
            except Exception as err:
                _LOGGER.warning("Sendspin connection error: %s", err)
            finally:
                if self._client is client:
                    self._client = None
                self._stream_active = False
                self._cleanup_fifo()

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    # ------------------------------------------------------------------
    # Client factory (stable identity)
    # ------------------------------------------------------------------

    def _create_client(self) -> SendspinClient:
        """Create a SendspinClient with the current official public API."""
        return SendspinClient(
            client_id=self._client_id,
            client_name=self._client_name,
            roles=[Roles.PLAYER],
            player_support=ClientHelloPlayerSupport(
                supported_formats=[
                    SupportedAudioFormat(
                        codec=AudioCodec.PCM,
                        sample_rate=48000,
                        bit_depth=16,
                        channels=2,
                    )
                ],
                buffer_capacity=500,
                supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
            ),
            device_info=DeviceInfo(
                product_name="Linux Voice Assistant",
                manufacturer="Open Home Foundation",
            ),
            initial_volume=100,
            state_supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
        )

    def _attach_callbacks(self, client: SendspinClient) -> None:
        """Attach audio and command callbacks."""
        client.add_audio_chunk_listener(self._on_audio_chunk)
        client.add_stream_start_listener(self._on_stream_start)
        client.add_stream_end_listener(self._on_stream_end)
        client.add_server_command_listener(self._on_server_command)
        client.add_disconnect_listener(self._on_disconnect)

    # ------------------------------------------------------------------
    # Audio & event handlers
    # ------------------------------------------------------------------

    def _on_audio_chunk(self, timestamp_us: int, audio_data: bytes, audio_format) -> None:
        if self._fifo_fd is None:
            return
        try:
            os.write(self._fifo_fd, audio_data)
        except OSError as err:
            self._log.debug("FIFO write error: %s", err)

    def _on_stream_start(self, message: StreamStartMessage) -> None:
        self._log.info("Sendspin stream start")
        self._stream_active = True

        self._player.stop()
        self._player._done_callback = None  # type: ignore[attr-defined]
        self._reset_fifo()

        self._player._mpv["demuxer-rawaudio-rate"] = 48000
        self._player._mpv["demuxer-rawaudio-channels"] = 2
        self._player._mpv["demuxer-rawaudio-format"] = "s16"

        self._player._mpv.pause = False
        self._player._mpv.play(str(self._fifo_path))

    def _on_stream_end(self, roles: Optional[List[str]]) -> None:
        self._log.info("Sendspin stream end")
        self._stream_active = False
        self._cleanup_fifo()

    def _on_server_command(self, payload) -> None:
        if hasattr(payload, "volume") and payload.volume is not None:
            self.set_volume(payload.volume)
        if hasattr(payload, "mute") and payload.mute is not None:
            self._player._mpv.mute = payload.mute

    def _on_disconnect(self) -> None:
        _LOGGER.info("Sendspin disconnected")
        self._stream_active = False
        self.stop()

    # ------------------------------------------------------------------
    # MpvMediaPlayer-compatible interface
    # ------------------------------------------------------------------

    def play(
        self,
        url: Union[str, List[str]],
        done_callback: Optional[Callable[[], None]] = None,
        stop_first: bool = False,
    ) -> None:
        if isinstance(url, list):
            url = url[0] if url else ""

        if self._stream_active:
            self._log.debug("Stopping Sendspin to play local URL: %s", url)
            self._stream_active = False
            self._cleanup_fifo()
            self._setup_fifo()

        self._player.play(url, done_callback=done_callback, stop_first=stop_first)

    def pause(self) -> None:
        self._player.pause()

    def resume(self) -> None:
        self._player.resume()

    def stop(self) -> None:
        self._player.stop()

    @property
    def is_playing(self) -> bool:
        return self._player.state() == PlayerState.PLAYING

    def set_volume(self, volume: float) -> None:
        self._player.set_volume(volume)

    def duck(self, factor: float = 0.5) -> None:
        self._player.duck(factor)

    def unduck(self) -> None:
        self._player.unduck()
