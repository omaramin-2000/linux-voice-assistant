"""Media player using mpv in a subprocess."""

import logging
from threading import Lock
from typing import Callable, List, Optional, Union

import numpy as np

_LOGGER = logging.getLogger(__name__)


class EnhancedMpvMediaPlayer:
    """
    Enhanced media player using AudioMixer.
    Compatible with existing MpvMediaPlayer interface.
    """

    def __init__(self, mixer, is_announcement: bool = False, device: Optional[str] = None):
        self.mixer = mixer
        self.is_announcement = is_announcement
        
        # State tracking (maintains compatibility)
        self.is_playing = False
        self.is_paused = False
        
        # Playlist management
        self._playlist: List[str] = []
        self._done_callback: Optional[Callable[[], None]] = None
        self._done_callback_lock = Lock()
        
        # Volume control (matches MpvMediaPlayer)
        self._duck_ratio: float = 0.5
        self._unduck_volume: int = 100
        self._duck_volume: int = self._compute_duck_volume()
        
        # Pause state
        self._paused_position = 0
        self._current_audio: Optional[np.ndarray] = None

    def play(
        self,
        url: Union[str, List[str]],
        done_callback: Optional[Callable[[], None]] = None,
        stop_first: bool = True,
    ) -> None:
        """Play audio file(s)."""
        if stop_first:
            self.stop()

        if isinstance(url, str):
            self._playlist = [url]
        else:
            self._playlist = list(url)

        with self._done_callback_lock:
            self._done_callback = done_callback
            
        self.is_paused = False
        self._play_next()

    def _play_next(self) -> None:
        """Play next item in playlist."""
        if not self._playlist:
            self.is_playing = False
            self.is_paused = False
            self._current_audio = None
            
            # Call done callback
            with self._done_callback_lock:
                if self._done_callback:
                    callback = self._done_callback
                    self._done_callback = None
                    try:
                        callback()
                    except Exception:
                        _LOGGER.exception("Error in done callback")
            return

        next_url = self._playlist.pop(0)
        _LOGGER.debug("Playing: %s", next_url)

        try:
            # Load audio file
            import soundfile as sf
            audio_data, sample_rate = sf.read(next_url, dtype="float32")

            # Resample if needed
            if sample_rate != self.mixer.sample_rate:
                try:
                    from scipy import signal
                    num_samples = int(len(audio_data) * self.mixer.sample_rate / sample_rate)
                    audio_data = signal.resample(audio_data, num_samples)
                    _LOGGER.debug("Resampled %s from %dHz to %dHz", next_url, sample_rate, self.mixer.sample_rate)
                except ImportError:
                    _LOGGER.warning(
                        "scipy not available, audio may play at wrong speed "
                        "(expected %dHz, got %dHz)",
                        self.mixer.sample_rate,
                        sample_rate,
                    )

            # Store for pause/resume
            self._current_audio = audio_data
            self._paused_position = 0

            # Ensure correct shape
            if len(audio_data.shape) == 1:
                # Mono - will be converted to stereo in mixer
                pass
            elif len(audio_data.shape) == 2:
                if audio_data.shape[1] > 2:
                    # More than stereo - take first 2 channels
                    audio_data = audio_data[:, :2]

            # Play through mixer
            self.is_playing = True
            self.is_paused = False
            
            if self.is_announcement:
                self.mixer.play_announcement(audio_data, done_callback=self._play_next)
            else:
                self.mixer.play_media(audio_data, done_callback=self._play_next)

        except Exception:
            _LOGGER.exception("Failed to play: %s", next_url)
            # Continue to next item on error
            self._play_next()

    def pause(self) -> None:
        """Pause playback."""
        if not self.is_playing:
            return
            
        was_active = self.is_playing or self.is_paused
        self.is_playing = False
        self.is_paused = was_active
        
        # Stop current playback
        if self.is_announcement:
            self.mixer.stop_announcement()
        else:
            self.mixer.stop_media()
        
        _LOGGER.debug("Paused (%s)", "announcement" if self.is_announcement else "media")

    def resume(self) -> None:
        """Resume playback."""
        if not self.is_paused:
            return
            
        was_paused = self.is_paused
        self.is_paused = False
        
        if was_paused and self._current_audio is not None:
            self.is_playing = True
            # Resume from paused position
            remaining_audio = self._current_audio[self._paused_position:]
            
            if self.is_announcement:
                self.mixer.play_announcement(remaining_audio, done_callback=self._play_next)
            else:
                self.mixer.play_media(remaining_audio, done_callback=self._play_next)
                
            _LOGGER.debug("Resumed (%s)", "announcement" if self.is_announcement else "media")
        elif self._playlist:
            self.is_playing = True
            self._play_next()
        else:
            self.is_playing = False

    def stop(self) -> None:
        """Stop playback."""
        self._playlist.clear()
        self.is_playing = False
        self.is_paused = False
        self._current_audio = None
        self._paused_position = 0

        if self.is_announcement:
            self.mixer.stop_announcement()
        else:
            self.mixer.stop_media()
            
        # Clear callback
        with self._done_callback_lock:
            self._done_callback = None

    def duck(self) -> None:
        """Duck volume (only for media player)."""
        if not self.is_announcement:
            # Calculate dB reduction from duck ratio
            # duck_ratio of 0.5 means -6dB
            db_reduction = -20 * np.log10(max(self._duck_ratio, 0.01))
            self.mixer.apply_ducking(db_reduction, duration=0.5)

    def unduck(self) -> None:
        """Restore volume (only for media player)."""
        if not self.is_announcement:
            self.mixer.apply_ducking(0, duration=1.0)  # Smooth 1s transition

    def set_volume(self, volume: int) -> None:
        """Set volume (0-100) - matches MpvMediaPlayer interface."""
        volume = max(0, min(100, volume))
        
        # Update internal tracking
        self._unduck_volume = volume
        self._duck_volume = self._compute_duck_volume()
        
        # Convert to 0.0-1.0 range and apply to mixer
        volume_normalized = volume / 100.0
        
        if self.is_announcement:
            self.mixer.set_announcement_volume(volume_normalized)
        else:
            self.mixer.set_media_volume(volume_normalized)
            
        _LOGGER.debug("Volume set to %d%% (%s)", volume, 
                     "announcement" if self.is_announcement else "media")

    def _compute_duck_volume(self) -> int:
        """Calculate ducked volume level."""
        if self._unduck_volume <= 0:
            return 0
        return max(1, int(round(self._unduck_volume * self._duck_ratio)))
