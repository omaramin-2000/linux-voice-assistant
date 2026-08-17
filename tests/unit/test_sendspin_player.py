"""Regression tests for the Sendspin media player adapter."""

from __future__ import annotations

from aiosendspin.models.types import AudioCodec, PlayerCommand, Roles

from linux_voice_assistant.player import sendspin_player


class _DummyLibMpvPlayer:
    def __init__(self, device=None, rawaudio: bool = False) -> None:
        self.device = device
        self.rawaudio = rawaudio


def test_sendspin_client_matches_current_aiosendspin_contract(monkeypatch) -> None:
    """The adapter must build a SendspinClient using the current public API."""
    monkeypatch.setattr(sendspin_player, "LibMpvPlayer", _DummyLibMpvPlayer)

    player = sendspin_player.SendspinMediaPlayer(
        device="default",
        client_id="test-client",
        client_name="Test Speaker",
        listen_port=8928,
    )

    client = player._create_client()

    assert client._client_id == "test-client"
    assert client._client_name == "Test Speaker"
    assert Roles.PLAYER in client._roles
    assert client._player_support.supported_formats[0].codec == AudioCodec.PCM
    assert client._player_support.supported_formats[0].sample_rate == 48000
    assert client._player_support.supported_formats[0].channels == 2
    assert PlayerCommand.VOLUME in client._state_supported_commands
    assert PlayerCommand.MUTE in client._state_supported_commands
