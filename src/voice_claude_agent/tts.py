"""Text-to-Speech abstraction layer."""

from typing import Protocol


class Speaker(Protocol):
    """Abstract TTS interface. Implementations must provide speak()."""

    def speak(self, text: str) -> None: ...


class MacOSSaySpeaker:
    """TTS using macOS `say` command."""

    def speak(self, text: str) -> None:
        import subprocess

        # Escape quotes in text
        safe_text = text.replace('"', '\\"')
        try:
            subprocess.run(
                ["say", safe_text],
                capture_output=True,
                timeout=30,
            )
        except FileNotFoundError:
            pass
        except subprocess.TimeoutExpired:
            pass


class FakeSpeaker:
    """Fake speaker for testing. Records spoken text."""

    def __init__(self) -> None:
        self.spoken: list[str] = []

    def speak(self, text: str) -> None:
        self.spoken.append(text)
