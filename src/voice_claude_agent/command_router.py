"""Command router — routes user input to appropriate handlers.

Maps transcribed intents to Claude CLI actions.
"""


def route_command(prompt: str) -> dict:
    """Classify and route a user command.

    Returns a routing decision dict. In MVP, all commands go to Claude CLI.
    """
    return {
        "prompt": prompt,
        "action": "run_claude_task",
        "risk_level": "read_only",  # Will be classified downstream
    }
