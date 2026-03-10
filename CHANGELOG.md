# Changelog

tbc

## Unreleased

- Add support for custom/external wake words
- Add `--download-dir <DIR>` to store downloaded wake word models/configs
- Switch to `soundcard` instead of `sounddevice`
- Add `--list-input-devices` and `--list-output-devices`
- Use `pymicro-wakeword` for microWakeWord
- Add zeroconf/mDNS discovery
- Support openWakeWord with `pyopen-wakeword`
- Support multiple wake words
- Save active wake words to preferences JSON file
- Refactor main into separate files

## 0.1.0 (2026-03-10)


### Bug Fixes

* **config:** remove unix: prefix from default pulseaudio socket path … ([#221](https://github.com/omaramin-2000/linux-voice-assistant/issues/221)) ([d6b803e](https://github.com/omaramin-2000/linux-voice-assistant/commit/d6b803e6d66c081221675bc212a76d1eaa3916e6))
* cookie error ([#127](https://github.com/omaramin-2000/linux-voice-assistant/issues/127)) ([#210](https://github.com/omaramin-2000/linux-voice-assistant/issues/210)) ([b53678e](https://github.com/omaramin-2000/linux-voice-assistant/commit/b53678e0f3cc3f22c7fb37c3208bf2ee0f9bd833))
* ensure wake word state is set after the wake sound ([#198](https://github.com/omaramin-2000/linux-voice-assistant/issues/198)) ([68d2890](https://github.com/omaramin-2000/linux-voice-assistant/commit/68d2890fddd7923fa4849b903a53238d863144d3))
* **script:** use os.environ instead of subprocess.environ in setup script ([#205](https://github.com/omaramin-2000/linux-voice-assistant/issues/205)) ([1104614](https://github.com/omaramin-2000/linux-voice-assistant/commit/1104614608102c15754836f1a4551ebea34ff5bc))
* Suppress spurious end-file event when starting playback and tts.speak ([#179](https://github.com/omaramin-2000/linux-voice-assistant/issues/179)) ([80e0d62](https://github.com/omaramin-2000/linux-voice-assistant/commit/80e0d6254eada0b32fbb500d8d66178f4261ad32))


### Documentation

* add documentation for low power device setup and improve setup script ([#206](https://github.com/omaramin-2000/linux-voice-assistant/issues/206)) ([efbad18](https://github.com/omaramin-2000/linux-voice-assistant/commit/efbad18275c52aec1d788d5f25a1986599be0cf7))
* add info about reboot ([#175](https://github.com/omaramin-2000/linux-voice-assistant/issues/175)) ([5a732f8](https://github.com/omaramin-2000/linux-voice-assistant/commit/5a732f8421039eb231adbd47db1c025a20c63827))
* added parameters ([5d3d824](https://github.com/omaramin-2000/linux-voice-assistant/commit/5d3d82419e7a0c013d9762e9ca87a3e130a438ce))

## 1.0.0

- Initial release
