"""Claude CLI output summarizer.

Generates concise user-facing text suitable for TTS readout.
Long outputs are truncated at natural sentence boundaries for voice-friendliness.
"""

import re

MAX_RESULT_CHARS_FOR_READOUT = 300  # voice-friendly limit (was 500)
_TTS_TRUNCATION_NOTE = "（回复较长，完整内容可在菜单栏 Last Summary 查看）"


def _strip_code_blocks(text: str) -> str:
    """Remove code fences and their content for TTS readability."""
    # Remove triple-backtick fenced blocks
    text = re.sub(r"```[\s\S]*?```", "[代码块已省略]", text)
    # Remove inline backticks
    text = re.sub(r"`[^`]+`", "[引用]", text)
    return text


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """Truncate text at the last sentence-ending punctuation within max_chars."""
    if len(text) <= max_chars:
        return text
    chunk = text[:max_chars]
    # Find the last sentence boundary within the chunk.
    matches = list(re.finditer(r"[。！？.!?]", chunk))
    if matches:
        return chunk[:matches[-1].end()].rstrip()
    # Fallback: truncate at last space
    last_space = chunk.rfind(" ")
    if last_space > max_chars // 2:
        return chunk[:last_space].rstrip()
    return chunk.rstrip()


def summarize(result_text: str, exit_code: int, duration_seconds: float) -> str:
    if exit_code == -2:
        return "Claude CLI 未找到，请确认已安装 Claude。"

    if exit_code == -1:
        return "Claude CLI 执行超时，请检查任务或重试。"

    if exit_code != 0:
        preview = result_text.strip()[:200]
        if not preview:
            return f"Claude CLI 执行失败，退出码 {exit_code}，无输出。"
        return f"Claude CLI 执行失败，退出码 {exit_code}。输出预览：{preview}"

    lines = [line for line in result_text.strip().split("\n") if line.strip()]
    if not lines:
        return "Claude CLI 执行完成，但无输出内容。"

    # Strip code blocks for TTS readability
    clean = _strip_code_blocks(result_text)

    if len(clean) <= MAX_RESULT_CHARS_FOR_READOUT:
        return clean.strip()

    truncated = _truncate_at_sentence(clean, MAX_RESULT_CHARS_FOR_READOUT)
    return f"{truncated}。{_TTS_TRUNCATION_NOTE}"


def summarize_for_record(result_text: str, exit_code: int, duration_seconds: float) -> str:
    """Generate the full summary persisted to logs and Last Summary."""
    if exit_code == 0 and result_text.strip():
        return result_text.strip()
    return summarize(result_text, exit_code, duration_seconds)
