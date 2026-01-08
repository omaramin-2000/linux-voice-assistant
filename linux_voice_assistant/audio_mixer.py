"""
Enhanced audio mixer with smooth ducking support.
Mixes media and announcement streams in real-time.
Compatible with existing MpvMediaPlayer interface.
"""

import logging
import threading
from queue import Empty, Queue
from typing import Callable, Optional

import numpy as np
import soundcard as sc

_LOGGER = logging.getLogger(__name__)


class AudioMixer:
    """Real-time audio mixer with smooth ducking support."""

    def __init__(
        self,
        sample_rate: int = 48000,
        channels: int = 2,
        device: Optional[str] = None,
        chunk_size: int = 1024,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_size = chunk_size

        # Audio queues for different streams
        self.media_queue: Queue = Queue(maxsize=200)
        self.announcement_queue: Queue = Queue(maxsize=200)

        # Ducking state
        self.ducking_level = 1.0  # Current level (1.0 = full volume)
        self.target_ducking = 1.0  # Target level
        self.ducking_speed = 0.05  # Transition speed per chunk (~50ms for full transition)

        # Volume control
        self.media_volume = 1.0
        self.announcement_volume = 1.0

        # Output device
        self.device = device
        self.speaker = None

        # Thread control
        self.running = False
        self.mixer_thread: Optional[threading.Thread] = None

        # Callbacks
        self.media_finished_callback: Optional[Callable[[], None]] = None
        self.announcement_finished_callback: Optional[Callable[[], None]] = None

        # Statistics
        self.underruns = 0

    def start(self) -> None:
        """Start the audio mixer."""
        if self.running:
            return

        self.running = True

        # Get output device
        if self.device:
            try:
                device_id = int(self.device)
                self.speaker = sc.get_speaker(device_id)
            except ValueError:
                # Try by name
                for speaker in sc.all_speakers():
                    if self.device.lower() in speaker.name.lower():
                        self.speaker = speaker
                        break
                if self.speaker is None:
                    _LOGGER.warning("Device '%s' not found, using default", self.device)
                    self.speaker = sc.default_speaker()
        else:
            self.speaker = sc.default_speaker()

        _LOGGER.info("Audio mixer output: %s (%dHz, %dch)", 
                    self.speaker.name, self.sample_rate, self.channels)

        # Start mixer thread
        self.mixer_thread = threading.Thread(target=self._mixer_loop, daemon=True)
        self.mixer_thread.start()

    def stop(self) -> None:
        """Stop the audio mixer."""
        if not self.running:
            return

        _LOGGER.debug("Stopping audio mixer")
        self.running = False

        # Clear queues
        self._clear_queue(self.media_queue)
        self._clear_queue(self.announcement_queue)

        # Signal threads to stop
        self.media_queue.put(None)
        self.announcement_queue.put(None)

        if self.mixer_thread and self.mixer_thread.is_alive():
            self.mixer_thread.join(timeout=2.0)

        if self.underruns > 0:
            _LOGGER.debug("Audio mixer had %d underruns", self.underruns)

    def _clear_queue(self, queue: Queue) -> None:
        """Clear all items from a queue."""
        while not queue.empty():
            try:
                queue.get_nowait()
            except Empty:
                break

    def _mixer_loop(self) -> None:
        """Main mixer loop that combines audio streams."""
        try:
            with self.speaker.player(
                samplerate=self.sample_rate,
                channels=self.channels,
                blocksize=self.chunk_size,
            ) as player:
                silence_chunks = 0
                max_silence = 50  # ~1 second of silence before logging

                while self.running:
                    # Mix audio from both streams
                    output = self._mix_audio_chunk()

                    if output is not None:
                        # Write to speaker
                        player.play(output)
                        silence_chunks = 0
                    else:
                        # No audio to play
                        silence_chunks += 1
                        if silence_chunks == max_silence:
                            _LOGGER.debug("Mixer running but no audio playing")
                        # Still need to write silence to keep stream alive
                        silence = np.zeros((self.chunk_size, self.channels), dtype=np.float32)
                        player.play(silence)

        except Exception:
            _LOGGER.exception("Error in mixer loop")
        finally:
            _LOGGER.debug("Mixer loop ended")

    def _mix_audio_chunk(self) -> Optional[np.ndarray]:
        """Mix a single chunk of audio from all streams."""
        output = np.zeros((self.chunk_size, self.channels), dtype=np.float32)
        has_audio = False

        # Smooth ducking transition
        if abs(self.ducking_level - self.target_ducking) > 0.001:
            if self.ducking_level < self.target_ducking:
                self.ducking_level = min(
                    self.ducking_level + self.ducking_speed, self.target_ducking
                )
            else:
                self.ducking_level = max(
                    self.ducking_level - self.ducking_speed, self.target_ducking
                )

        # Get media audio (with ducking applied)
        try:
            media_chunk = self.media_queue.get_nowait()
            if media_chunk is None:
                # End of media stream
                if self.media_finished_callback:
                    callback = self.media_finished_callback
                    self.media_finished_callback = None
                    # Call in thread to avoid blocking mixer
                    threading.Thread(target=callback, daemon=True).start()
            else:
                # Ensure correct shape
                if len(media_chunk.shape) == 1:
                    media_chunk = np.column_stack([media_chunk, media_chunk])
                elif media_chunk.shape[1] == 1:
                    media_chunk = np.column_stack([media_chunk, media_chunk])

                # Apply ducking, volume, and add to output
                chunk_len = min(len(media_chunk), self.chunk_size)
                volume_factor = self.ducking_level * self.media_volume
                output[:chunk_len] += media_chunk[:chunk_len] * volume_factor
                has_audio = True
        except Empty:
            pass

        # Get announcement audio (full volume, priority)
        try:
            announcement_chunk = self.announcement_queue.get_nowait()
            if announcement_chunk is None:
                # End of announcement stream
                if self.announcement_finished_callback:
                    callback = self.announcement_finished_callback
                    self.announcement_finished_callback = None
                    # Call in thread to avoid blocking mixer
                    threading.Thread(target=callback, daemon=True).start()
            else:
                # Ensure correct shape
                if len(announcement_chunk.shape) == 1:
                    announcement_chunk = np.column_stack([announcement_chunk, announcement_chunk])
                elif announcement_chunk.shape[1] == 1:
                    announcement_chunk = np.column_stack([announcement_chunk, announcement_chunk])

                # Apply volume and add to output at full volume (no ducking)
                chunk_len = min(len(announcement_chunk), self.chunk_size)
                output[:chunk_len] += announcement_chunk[:chunk_len] * self.announcement_volume
                has_audio = True
        except Empty:
            pass

        if not has_audio:
            self.underruns += 1
            return None

        # Prevent clipping with soft limiting
        output = np.clip(output, -1.0, 1.0)
        return output

    def play_media(
        self, audio_data: np.ndarray, done_callback: Optional[Callable[[], None]] = None
    ) -> None:
        """Queue media audio (music, etc.)."""
        if done_callback:
            self.media_finished_callback = done_callback

        # Split into chunks and queue
        for i in range(0, len(audio_data), self.chunk_size):
            chunk = audio_data[i : i + self.chunk_size]
            try:
                self.media_queue.put(chunk.astype(np.float32), timeout=1.0)
            except Exception:
                _LOGGER.warning("Media queue full, dropping audio")
                break

        # Signal end of stream
        self.media_queue.put(None)

    def play_announcement(
        self, audio_data: np.ndarray, done_callback: Optional[Callable[[], None]] = None
    ) -> None:
        """Queue announcement audio (voice assistant, alerts)."""
        if done_callback:
            self.announcement_finished_callback = done_callback

        # Split into chunks and queue
        for i in range(0, len(audio_data), self.chunk_size):
            chunk = audio_data[i : i + self.chunk_size]
            try:
                self.announcement_queue.put(chunk.astype(np.float32), timeout=1.0)
            except Exception:
                _LOGGER.warning("Announcement queue full, dropping audio")
                break

        # Signal end of stream
        self.announcement_queue.put(None)

    def apply_ducking(self, decibel_reduction: float, duration: float = 0.5) -> None:
        """
        Apply ducking to media stream with smooth transition.

        Args:
            decibel_reduction: Amount to reduce in dB (0 = full, 20 = -20dB)
            duration: Transition time in seconds (0 = instant)
        """
        # Convert dB to linear scale
        self.target_ducking = 10 ** (-decibel_reduction / 20)
        
        # Adjust transition speed based on duration
        if duration > 0:
            # Calculate speed needed to reach target in given duration
            # Each chunk is ~chunk_size/sample_rate seconds
            chunk_duration = self.chunk_size / self.sample_rate
            steps = max(1, int(duration / chunk_duration))
            self.ducking_speed = abs(self.target_ducking - self.ducking_level) / steps
        else:
            # Instant change
            self.ducking_level = self.target_ducking
            
        _LOGGER.debug(
            "Ducking: %.2f (%.1fdB) over %.1fs",
            self.target_ducking,
            -decibel_reduction,
            duration
        )

    def set_media_volume(self, volume: float) -> None:
        """Set media stream volume (0.0 to 1.0)."""
        self.media_volume = max(0.0, min(1.0, volume))
        _LOGGER.debug("Media volume: %.2f", self.media_volume)

    def set_announcement_volume(self, volume: float) -> None:
        """Set announcement stream volume (0.0 to 1.0)."""
        self.announcement_volume = max(0.0, min(1.0, volume))
        _LOGGER.debug("Announcement volume: %.2f", self.announcement_volume)

    def stop_media(self) -> None:
        """Stop media playback immediately."""
        self._clear_queue(self.media_queue)
        self.media_finished_callback = None

    def stop_announcement(self) -> None:
        """Stop announcement playback immediately."""
        self._clear_queue(self.announcement_queue)
        self.announcement_finished_callback = None

    def get_queue_sizes(self) -> tuple:
        """Get current queue sizes for debugging."""
        return (self.media_queue.qsize(), self.announcement_queue.qsize())
