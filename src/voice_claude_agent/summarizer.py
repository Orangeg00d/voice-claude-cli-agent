"""Claude CLI output summarizer.

Generates concise user-facing text suitable for TTS readout.
"""

MAX_RESULT_CHARS_FOR_READOUT = 500


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

    if len(result_text) <= MAX_RESULT_CHARS_FOR_READOUT:
        return result_text.strip()

    first_lines = lines[:3]
    preview = "\n".join(line.rstrip()[:120] for line in first_lines)
    return (
        f"回复较长，共 {len(result_text)} 字符。开头内容：{preview}"
    )
