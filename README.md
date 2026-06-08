# Voice Claude CLI Agent

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![macOS](https://img.shields.io/badge/platform-macOS-lightgrey)](https://www.apple.com/macos/)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-green)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-278%20passing-brightgreen)](tests/)

一个面向 macOS 的 local-first 语音 Agent 项目。目标是让用户通过语音唤醒发出开发指令，由本地 Claude CLI 执行任务，并在完成后用语音播报结果；STT/TTS 默认可本地运行，也可按需切换到 Volcengine/Doubao 云端后端。

## 快速开始

```bash
# 安装
./init.sh install          # 创建 .venv 并安装所有依赖

# 检查环境
./init.sh check            # Python, Claude CLI, macOS say, 包导入, agent_state
voice-claude-agent check   # 完整检查：含麦克风权限和 STT 后端

# 文本模式 — 无需麦克风
voice-claude-agent demo-text "请回复 OK"    # 完整演示：Claude → 摘要 → TTS (fake)
voice-claude-agent run-text "总结当前项目"   # 正式执行 + macOS say 播报

# 语音测试 — 无需麦克风 (使用 fake 组件)
voice-claude-agent demo-voice "帮我检查 git 状态"   # fake 录音 → STT → Claude → say
voice-claude-agent voice --fake                     # fake 录音 → mock STT → Claude → say
voice-claude-agent wake --fake --once               # 唤醒循环，单次迭代

# 真语音 — 需要麦克风权限
voice-claude-agent record                    # 纯录音 + 转写 (不执行 Claude)
voice-claude-agent voice                     # 录音 → STT → Claude → say
voice-claude-agent wake                      # 唤醒循环，Enter 触发，Ctrl+C 退出

# 菜单栏 App 录音时长（默认 5 秒）
VOICE_RECORD_SECONDS=3 voice-claude-agent app
launchctl setenv VOICE_RECORD_SECONDS 3      # Finder 双击 .app 前设置 GUI 环境

# 或写入本地配置文件，Finder 启动的 .app 也会读取
mkdir -p ~/.voice-claude-agent
cat > ~/.voice-claude-agent/config.json <<'JSON'
{
  "VOICE_RECORD_SECONDS": "10",
  "VOICE_STT_BACKEND": "whisper-cli",
  "WHISPER_CPP_MODEL": "/path/to/ggml-base.bin",
  "WHISPER_CPP_LANGUAGE": "zh"
}
JSON

# 开发
./init.sh test     # 运行测试
./init.sh lint     # ruff 检查
./init.sh format   # ruff 格式化
```

## Current Status

Phases 1-17 已完成，共 71 项验收 (F001-F071) 全部通过，278 个测试，lint clean。Phase 10 完成了真实使用验收中的稳定性、中文体验、连续触发保护与高风险动作语音确认；Phase 11 完成发布前审计、最终人工验收手册和开发者计划申请材料；Phase 12 完成 MIT License、仓库安全/贡献说明和依赖工具索引；Phase 13 完成 GitHub Release 草稿、README badges 和仓库元数据建议；Phase 14 增加菜单栏 Settings UI，用于查看、编辑、重置本地配置；Phase 15 增加 Volcengine/Doubao BigModel ASR Flash 云端语音识别后端；Phase 16 固定 Claude CLI 工作目录，避免语音指令进入错误项目上下文；Phase 17 增加 Volcengine/Doubao TTS 云端语音合成后端，并在失败时自动 fallback 到 macOS say。

核心能力：
- CLI 文本/语音命令执行，高风险动作二次确认；菜单栏语音流程支持说“同意/取消”确认高风险动作
- 菜单栏 App (rumps) + py2app .app 构建
- whisper.cpp 离线语音转文字
- Volcengine/Doubao 云端 ASR 和可选 TTS
- 录音诊断、错误恢复、并发安全
- 结构化日志 (app_events + sessions + last_result)
- `--fake` 模式无需麦克风即可测试

## 架构

```text
macOS 常驻进程 (CLI 原型)
  -> 按键/命令行触发 (wake --fake 或 Enter 手动唤醒)
  -> 录音 (sounddevice, 16kHz mono PCM)
  -> Speech-to-Text (可插拔后端)
  -> 风险分类 (只读 / 可恢复 / 破坏性)
  -> 确认 (高风险动作 yes/no)
  -> Claude CLI 执行 (claude -p, subprocess)
  -> 结果摘要
  -> Text-to-Speech (macOS say 或 Volcengine/Doubao TTS)
  -> JSONL 日志
```

## STT 后端

`--stt-backend` 选项控制语音转文字使用哪个后端。在 `voice`、`record`、`wake` 命令中均可用：

| 后端 | 说明 | 需要 |
|------|------|------|
| `text-input` (默认) | 打印录音统计，不真正转写。适合开发和调试。 | 无 |
| `whisper-cli` | 调用本地 whisper.cpp 二进制做离线转写。 | 安装 whisper.cpp + 下载 GGML 模型 |

### whisper-cli 前置条件

使用 `--stt-backend whisper-cli` 之前需要：

1. **安装 whisper.cpp**：`brew install whisper-cpp`
2. **下载 GGML 模型**：从 [huggingface.co/ggerganov/whisper.cpp](https://huggingface.co/ggerganov/whisper.cpp) 下载一个 `.bin` 文件（如 `ggml-base.en.bin`）
3. **设置环境变量**：`export WHISPER_CPP_MODEL=/path/to/ggml-base.en.bin`

注意：系统上的 `whisper` 命令可能是 Python `openai-whisper` 包，whisper-cli 后端会自动检测并拒绝使用。请确保安装的是 whisper.cpp 而不是 pip install openai-whisper。

```bash
# 完整安装流程
brew install whisper-cpp
mkdir -p ~/whisper-models
cd ~/whisper-models
curl -LO https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
export WHISPER_CPP_MODEL=~/whisper-models/ggml-base.en.bin

# 现在可以使用 whisper-cli 后端
voice-claude-agent voice --stt-backend whisper-cli
```
| `apple-speech` | 使用 macOS 内建听写引擎 (NSSpeechRecognizer via osascript)。 | 系统设置 > 键盘 > 听写 开关打开 |

### Apple Speech 说明与限制

Apple Speech 后端会：

1. 先检查系统听写开关 (`DictationIMEnabled`)。
2. 如果未开启，直接返回明确的错误提示并建议换后端。
3. 如果已开启，通过 AppleScript 播放 WAV 以触发听写。

**已知限制**：Apple Speech 的转写结果会出现在**当前活跃的文本输入框**中，而非程序化捕获。这意味着它适合「边说边打」的场景，但无法自动把转写文本送回 wake 循环。这是 macOS 内建听写 API 的限制。如果需要**完全自动化的语音转文字**，请安装 whisper.cpp 并使用 `--stt-backend whisper-cli`。

```bash
# 查看所有可用后端
voice-claude-agent check
# 示例输出 → STT backends: text-input, whisper-cli, apple-speech

# 尝试 Apple Speech（如果听写已开启）
voice-claude-agent record --stt-backend apple-speech

# 尝试 whisper.cpp（需要先安装）
brew install whisper-cpp
voice-claude-agent voice --stt-backend whisper-cli
```

## Fake / Real 模式

所有语音命令都有 `--fake` 选项，不依赖真实麦克风即可测试完整链路：

| 命令 | fake 模式行为 | real 模式行为 |
|------|-------------|-------------|
| `demo-voice` | 总是 fake：预设录音 + 预设 transcript | — |
| `voice --fake` | 用 FakeRecorder 录音 + mock STT 文本 | 真录音 → STT → Claude → say |
| `voice` | — | 检查麦克风权限 → 真录音 → STT → Claude → say |
| `wake --fake --once` | 自动触发 → 模拟录音 → 预设 transcript | — |
| `wake --fake` | 自动循环，Ctrl+C 退出 | — |
| `wake` | — | Enter 手动唤醒，循环录音 + STT + Claude |

```bash
# 无麦克风的完整测试
voice-claude-agent wake --fake --once

# 真麦克风 + 唤醒循环
voice-claude-agent wake
```

## 协同开发模式

本项目用于测试一种双 Agent 协同模式：

- Claude Desktop / Claude CLI：主要开发者。严格按照 `VOICE_CLAUDE_CLI_AGENT_PLAN.md`、`CLAUDE.md` 和 feature 清单逐步实现功能。
- Codex：审核、验收、审计、GitHub 发布者。Codex 不作为主要开发者，重点检查代码质量、测试、风险边界、Git 状态和下一步计划。

开发原则：

- 每轮 Claude 只做一个 feature。
- 每轮必须自测。
- 每轮必须更新进度文件。
- Codex 审核通过后再提交、推送或更新 GitHub。
- 如果实现偏离计划，Codex 需要把审计结论写回下一步工作计划。

## 关键文档

- `VOICE_CLAUDE_CLI_AGENT_PLAN.md`：完整开发任务书和阶段计划。
- `CLAUDE.md`：Claude Desktop / Claude CLI 的工作规则。
- `CODEX_REVIEW_GUIDE.md`：Codex 审核和验收规则。
- `CLAUDE_CLI_TASK_ORCHESTRATION.md`：Codex 如何编排 Claude CLI 执行小任务。
- `RELEASE_NOTES.md`：版本发布说明与功能清单。
- `PRIVACY.md`：隐私声明与数据处理说明。
- `SECURITY.md`：安全报告方式、支持版本和无遥测声明。
- `CONTRIBUTING.md`：Claude + Codex 协同开发与提交流程。
- `TOOLS_AND_DEPS.md`：依赖、系统工具和外部工具说明。
- `GITHUB_RELEASE_DRAFT.md`：GitHub v0.1.0 Release 草稿。
- `REPOSITORY_METADATA.md`：GitHub description、topics、homepage 等仓库设置建议。
- `MANUAL_TEST_PHASE14.md`：Settings UI 手动验收手册。
- `docs/VOLCENGINE_ASR_SETUP.md`：Volcengine/Doubao ASR 后端配置和安全说明。
- `MANUAL_TEST_PHASE8.md`：菜单栏 App 手动验收手册。
- `MANUAL_TEST_PHASE11.md`：最终人工验收手册（Health Check、Mic Diagnostic、语音指令、高风险确认、View Logs）。
- `DEVELOPER_PROGRAM_APPLICATION.md`：开发者计划申请材料。

## 开源许可

MIT License — 详见 [LICENSE](LICENSE)。欢迎通过 [CONTRIBUTING.md](CONTRIBUTING.md) 了解协同开发模式和提交流程。

## 已确认方向

- 平台：macOS。
- 第一版语言：Python。
- 第一版 TTS：macOS `say`；可选 Volcengine/Doubao TTS。
- 第一版 STT：可插拔接口，MVP 提供 text-input / whisper-cli / apple-speech 三种后端。
- 第一版唤醒：按键/命令行触发 (ManualWakeTrigger)。
- 最终形态：macOS 常驻 App。
- App 不需要开机自动启动。
- 允许 App 运行期间常驻监听麦克风。
- 高风险动作默认必须二次确认。

## 开发状态

Current main — F001-F071 (Phase 1-17) 全部通过。278 个测试。详情见 `feature_list.json`、`RELEASE_NOTES.md`。

v0.1.0 Release — F001-F067 (Phase 1-13)，223 个测试，已发布到 GitHub Releases。

## Manual Smoke Test（手动验收）

### 启动菜单栏 App

```bash
# 终端模式
voice-claude-agent app

# 或从 Finder 双击 dist/VoiceClaudeAgent.app（需先构建）
open dist/VoiceClaudeAgent.app
```

### 预期行为

| 菜单项 | 功能 | 预期 |
|--------|------|------|
| `Start Wake` | 开始后台语音唤醒循环 | 标题变为 `Start Wake (running)`，菜单图标不变 |
| `Stop Wake` | 停止唤醒循环 | 标题恢复为 `Stop Wake`，麦克风释放 |
| `Trigger Recording` | 单次录音 → STT → Claude → TTS | 状态栏依次显示：`Recording...` → `Transcribing...` → `Running Claude...` → `Done ✓`（1.5s 后恢复） |
| `Mic Diagnostic` | 显示麦克风权限和设备详情 | 弹出诊断窗口，含 bundle ID、录音时长、输入设备、PortAudio 状态 |
| `Mic Status` | 当前麦克风权限 | `Mic: Accessible` 或 `Mic: Denied` |
| `Last Transcript` | 上一次识别的语音文本 | 点击弹出 rumps 对话框 |
| `Last Summary` | 上一次 Claude 回答的完整摘要 | 点击弹出完整文本（不会被截断） |
| `Quit` | 退出 | 先停止 wake loop |

### 已知限制

- **TCC / 麦克风权限**：`dist/VoiceClaudeAgent.app` 包含 `NSMicrophoneUsageDescription`，需要被 macOS 识别。首次启动如有权限问题，运行 `tccutil reset Microphone com.voiceclaude.agent` 后在 Finder 中重新打开。
- **PortAudio 动态库**：py2app 构建脚本会自动把 `libportaudio.dylib` 提取到文件系统。如遇 `PortAudio unavailable` 错误，请确认已运行 `python setup.py py2app`（非 `-A` 别名模式）。
- **TTS 截断**：长回答只播报前半部分 + "完整内容可在菜单栏 Last Summary 查看"，完整文本保留在 `sessions.jsonl` 和 `Last Summary`。
- **非语音唤醒词**：当前触发方式为手动点击 Trigger Recording 或 Start Wake，不支持真实语音唤醒词。
