"""Audio recorder — Phase 2 real microphone recording.

Provides the AudioRecorder protocol, a real SoundDevice implementation,
and a FakeRecorder for tests.
"""

from dataclasses import dataclass, field
from typing import Protocol


class AudioRecorder(Protocol):
    """Abstract audio recorder interface."""

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def is_recording(self) -> bool: ...

    def get_audio(self) -> bytes: ...


@dataclass
class SoundDeviceRecorder:
    """Real microphone recorder using the sounddevice + numpy stack.

    Uses a push-to-talk model: call start(), then stop(), then get_audio().
    """

    sample_rate: int = 16000
    channels: int = 1
    device: int | None = None

    _frames: list = field(default_factory=list, init=False)
    _stream: object = field(default=None, init=False, repr=False)
    _recording: bool = field(default=False, init=False)

    def start(self) -> None:
        import sounddevice as sd
        import numpy as np

        if self._recording:
            return
        self._frames = []
        self._recording = True

        def _callback(indata, frames, time_info, status):
            if self._recording:
                self._frames.append(indata.copy())

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            device=self.device,
            callback=_callback,
            dtype=np.float32,
        )
        self._stream.start()

    def stop(self) -> None:
        if not self._recording:
            return
        self._recording = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def is_recording(self) -> bool:
        return self._recording

    def get_audio(self) -> bytes:
        import numpy as np

        if not self._frames:
            return b""
        audio = np.concatenate(self._frames, axis=0)
        # Convert float32 [-1,1] to int16 PCM
        audio_int16 = (audio * 32767).astype(np.int16)
        return audio_int16.tobytes()


class FakeRecorder:
    """Fake recorder for testing. Returns preset audio bytes."""

    def __init__(self, audio: bytes = b"") -> None:
        self._audio = audio
        self._recording = False
        self.start_calls: list[None] = []
        self.stop_calls: list[None] = []

    def start(self) -> None:
        self._recording = True
        self.start_calls.append(None)

    def stop(self) -> None:
        self._recording = False
        self.stop_calls.append(None)

    def is_recording(self) -> bool:
        return self._recording

    def get_audio(self) -> bytes:
        return self._audio
