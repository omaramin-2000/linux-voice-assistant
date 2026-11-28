"""Tests for the pipeline start requests in linux_voice_assistant/satellite.py (audio settings sent to Home Assistant)."""

from unittest.mock import MagicMock

from aioesphomeapi.model import VoiceAssistantAudioSettings, VoiceAssistantCommand

from linux_voice_assistant.satellite import _pipeline_start_request
from tests.unit.conftest import make_satellite

# Without explicit audio settings Home Assistant multiplies the audio by 0.0


def _sent_request(sat):
    return sat.send_messages.call_args.args[0][0]


def test_request_starts_a_pipeline_with_the_wake_word_phrase():
    request = _pipeline_start_request("Okay Nabu")

    assert request.start
    assert request.wake_word_phrase == "Okay Nabu"


def test_request_has_an_empty_phrase_without_a_wake_word():
    assert _pipeline_start_request("").wake_word_phrase == ""


def test_audio_settings_are_sent_explicitly():
    request = _pipeline_start_request("Okay Nabu")

    assert request.HasField("audio_settings")
    assert request.audio_settings.volume_multiplier == 1.0
    assert request.audio_settings.noise_suppression_level == 0
    assert request.audio_settings.auto_gain == 0


def test_home_assistant_receives_audio_settings_that_leave_the_audio_unchanged():
    # Home Assistant reads the request through aioesphomeapi
    command = VoiceAssistantCommand.from_pb(_pipeline_start_request("Okay Nabu"))

    assert command.audio_settings == VoiceAssistantAudioSettings(noise_suppression_level=0, auto_gain=0, volume_multiplier=1.0)


def test_streaming_start_sends_volume_multiplier_one(tmp_path):
    sat = make_satellite(tmp_path)
    sat.send_messages = MagicMock()

    sat._start_audio_streaming("Hey Jarvis")

    assert _sent_request(sat).audio_settings.volume_multiplier == 1.0


def test_start_after_wake_sound_sends_volume_multiplier_one(tmp_path):
    sat = make_satellite(tmp_path)
    sat.send_messages = MagicMock()

    sat._on_wakeup_sound_finished("Hey Jarvis")

    assert _sent_request(sat).audio_settings.volume_multiplier == 1.0


def test_start_after_button_sends_volume_multiplier_one(tmp_path):
    sat = make_satellite(tmp_path)
    sat.send_messages = MagicMock()

    sat._on_start_listening_sound_finished()

    assert _sent_request(sat).audio_settings.volume_multiplier == 1.0
