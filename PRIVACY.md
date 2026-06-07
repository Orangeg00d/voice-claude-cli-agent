# Privacy Statement — Voice Claude Agent

## 数据本地化

Voice Claude Agent **完全在本地运行**，所有数据处理不离开你的 Mac：

| 数据类型 | 处理位置 | 传输 |
|----------|----------|------|
| 麦克风录音 | 本地 (sounddevice) | 不传输 |
| 语音转文字 | 本地 (whisper.cpp) | 不传输 |
| 文字指令 | 本地 | 不传输 |
| Claude CLI 执行 | 本地 (claude) | 取决于 Claude CLI 配置 |
| 结果摘要 | 本地 | 不传输 |
| 语音播报 | 本地 (macOS say) | 不传输 |
| 运行日志 | 本地文件系统 | 不传输 |

## 数据存储

- **`agent_state/sessions.jsonl`**: 每次执行的 transcript、摘要、退出码
- **`agent_state/last_result.json`**: 最新一次执行结果
- **`agent_state/app_events.jsonl`**: 运行时事件日志 (触发、录音、STT、Claude 的时间点)
- **`agent_state/app-events.log`**: 详细运行时日志

以上数据仅存储在项目目录或 `~/Library/Application Support/VoiceClaudeAgent/agent_state/` 中，可通过 `VOICE_CLAUDE_AGENT_STATE_DIR` 环境变量自定义路径。

## 麦克风权限

- 首次使用麦克风时，macOS 会弹出权限请求
- 授予权限后可在 System Settings > Privacy & Security > Microphone 中管理
- 可随时撤销，撤销后 App 将显示 "Mic: Denied" 且无法录音
- 麦克风数据仅在录音持续期间存在于内存中，录音结束即释放

## 网络请求

- Voice Claude Agent 本身**不发起任何网络请求**
- whisper.cpp 模型文件需自行下载到本地
- Claude CLI 可能根据其自身配置发起网络请求（请参考 Claude CLI 的隐私政策）

## 开源承诺

本项目代码完全公开，可审计数据处理流程。欢迎通过 GitHub Issues 或 PR 提出隐私相关的改进建议。
