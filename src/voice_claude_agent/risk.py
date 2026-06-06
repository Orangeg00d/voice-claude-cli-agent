"""Three-level risk classifier.

Categories:
- read_only: viewing state, reading files, summarizing code
- recoverable: modifying local code, creating branches, running tests
- external_or_destructive: push, deploy, delete, merge, production changes
"""

from enum import Enum

DESTRUCTIVE_KEYWORDS = [
    "push",
    "deploy",
    "delete",
    "remove",
    "rm -rf",
    "rm -r",
    "merge",
    "reset --hard",
    "drop table",
    "truncate",
    "--force",
    "-f ",
    "production",
    "secret",
    "token",
    "credential",
    "--dangerously-skip-permissions",
]

RECOVERABLE_KEYWORDS = [
    "modify",
    "change",
    "update",
    "edit",
    "write",
    "create",
    "add",
    "install",
    "build",
    "compile",
    "test",
    "refactor",
    "rename",
    "move",
    "copy",
    "branch",
    "commit",
    "rebase",
]


class RiskLevel(str, Enum):
    READ_ONLY = "read_only"
    RECOVERABLE = "recoverable"
    EXTERNAL_OR_DESTRUCTIVE = "external_or_destructive"


def classify_risk(prompt: str) -> RiskLevel:
    lowered = prompt.lower()

    for kw in DESTRUCTIVE_KEYWORDS:
        if kw.lower() in lowered:
            return RiskLevel.EXTERNAL_OR_DESTRUCTIVE

    for kw in RECOVERABLE_KEYWORDS:
        if kw.lower() in lowered:
            return RiskLevel.RECOVERABLE

    return RiskLevel.READ_ONLY


def requires_confirmation(risk_level: RiskLevel) -> bool:
    return risk_level == RiskLevel.EXTERNAL_OR_DESTRUCTIVE
