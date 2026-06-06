"""Wake trigger abstraction layer.

Phase 3 feature — MVP provides the interface and a manual/CLI trigger.
"""

from typing import Protocol


class WakeTrigger(Protocol):
    """Abstract wake trigger. Implementations wait for activation and return."""

    def wait_for_wake(self) -> bool: ...


class ManualWakeTrigger:
    """Manual trigger: press Enter or call trigger() to activate.

    This is the MVP wake trigger — it replaces real wake-word detection
    until Phase 3.
    """

    def __init__(self, auto_trigger: bool = False) -> None:
        self.auto_trigger = auto_trigger
        self.trigger_count = 0

    def wait_for_wake(self) -> bool:
        if self.auto_trigger:
            self.trigger_count += 1
            return True
        try:
            input("Press Enter to wake...")
            self.trigger_count += 1
            return True
        except (EOFError, KeyboardInterrupt):
            return False

    def trigger(self) -> None:
        """Programmatic trigger for tests and CLI mode."""
        self.trigger_count += 1
