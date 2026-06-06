"""Speech-to-Text abstraction layer.

Phase 2 feature — MVP provides the interface and a text-input/mock fallback.
"""

from typing import Protocol


class Transcriber(Protocol):
    """Abstract STT interface. Implementations convert audio to text."""

    def transcribe(self, audio_data: bytes | None = None) -> str: ...


class TextInputTranscriber:
    """Development transcriber: returns text directly (no audio processing)."""

    def transcribe(self, audio_data: bytes | None = None) -> str:
        if audio_data is not None:
            return f"[mock STT: {len(audio_data)} bytes of audio]"
        return input("Enter command text: ").strip()


class FakeTranscriber:
    """Fake transcriber for testing. Returns a preset response."""

    def __init__(self, response: str = "") -> None:
        self.response = response
        self.calls: list[bytes | None] = []

    def transcribe(self, audio_data: bytes | None = None) -> str:
        self.calls.append(audio_data)
        return self.response
