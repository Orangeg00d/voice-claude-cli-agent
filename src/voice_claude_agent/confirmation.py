"""Risk confirmation handler. MVP uses CLI yes/no input."""


def ask_confirmation(prompt: str) -> bool:
    """Ask user to confirm a high-risk action. Returns True if confirmed."""
    print()
    print("=" * 60)
    print("HIGH-RISK ACTION DETECTED")
    print("=" * 60)
    print(f"Action: {prompt}")
    print("This action could affect external systems or be irreversible.")
    print()
    response = input("Proceed? (yes/no): ").strip().lower()
    confirmed = response in ("yes", "y")
    print(f"{'Confirmed' if confirmed else 'Rejected'} by user.")
    return confirmed


def confirm_or_reject(
    prompt: str,
    risk_level: str,
    confirmation_fn=ask_confirmation,
) -> tuple[bool, str]:
    """Check risk and request confirmation if needed.

    Returns (proceed, reason).
    """
    from voice_claude_agent.risk import requires_confirmation, RiskLevel

    if not requires_confirmation(RiskLevel(risk_level)):
        return True, f"Risk level '{risk_level}' does not require confirmation."

    if confirmation_fn(prompt):
        return True, "User confirmed the high-risk action."
    else:
        return False, "User rejected the high-risk action."


# ── F064: Voice-based confirmation keywords ────────────────

_CONFIRM_ACCEPT: set[str] = {
    "同意", "确认", "继续", "可以", "好", "行", "是的", "是", "对",
    "yes", "ok", "y", "go", "do it", "proceed", "confirm",
}
_CONFIRM_REJECT: set[str] = {
    "取消", "不要", "拒绝", "不行", "不", "否",
    "no", "cancel", "n", "abort", "stop",
}


def is_voice_confirm(transcript: str) -> bool | None:
    """Check if a voice transcript means confirm/accept.

    Returns True=yes, False=no, None=unclear (try again).
    """
    clean = transcript.strip().lower()
    # Check reject first (longer, more specific)
    for kw in sorted(_CONFIRM_REJECT, key=len, reverse=True):
        if kw in clean:
            return False
    for kw in sorted(_CONFIRM_ACCEPT, key=len, reverse=True):
        if kw in clean:
            return True
    return None
