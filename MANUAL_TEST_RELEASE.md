# Voice Claude Agent — Release Acceptance Test (Phase 9)

本文档覆盖从零安装到首个语音命令成功的完整验收步骤。
执行人应在干净的 macOS 机器（或从未运行过 Voice Claude Agent 的用户账号）上按步骤操作。

## 0. 前置条件

- macOS 14+ (Sonoma or later)
- 已安装 Homebrew
- 已安装 Claude CLI (`brew install claude` 或等效)
- 已登录 Claude CLI

## 1. 安装项目

```bash
git clone <repo-url> voice-claude-agent
cd voice-claude-agent
./init.sh install
```

### 验收标准
- [ ] `./init.sh check` 输出 `All critical checks passed`
- [ ] `./init.sh test` 显示所有测试通过
- [ ] `voice-claude-agent --help` 列出所有命令

## 2. 安装 whisper.cpp

```bash
brew install whisper-cpp
mkdir -p ~/whisper-models
curl -L -o ~/whisper-models/ggml-base.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin
export WHISPER_CPP_MODEL=~/whisper-models/ggml-base.bin
whisper-cli -m $WHISPER_CPP_MODEL -h | head -5
```

### 验收标准
- [ ] `whisper-cli` 命令存在
- [ ] `WHISPER_CPP_MODEL` 指向的模型文件存在且可以读取
- [ ] `whisper-cli -h` 输出帮助信息

## 3. 首次麦克风授权

```bash
voice-claude-agent app --stt-backend whisper-cli
```

### 验收标准
- [ ] 如果 macOS 弹出麦克风权限请求，点击 "允许"（Allow）
- [ ] 菜单栏出现 🎤 图标
- [ ] 点击 `Health Check`，确认 `Microphone: [PASS]`
- [ ] 如果 `Microphone: [FAIL]`，打开 System Settings > Privacy & Security > Microphone，启用 Voice Claude Agent

## 4. 首次 Trigger Recording（说话 → 转写 → Claude → 播报）

### 步骤
1. 点击 `Trigger Recording`
2. 对着麦克风清晰地说一句话，例如 "请用一句话介绍你自己"
3. 等待录音完成（默认 5 秒），观察状态变化：`Recording...` → `Transcribing...` → `Running Claude...` → `Done ✓`
4. 听 macOS say 的语音播报

### 验收标准
- [ ] 状态栏依次变化，不卡住
- [ ] 听到 Claude 的回答通过 macOS say 播出
- [ ] 点击 `Last Transcript`，看到你的语音被正确转写
- [ ] 点击 `Last Summary`，看到 Claude 的完整回答文本
- [ ] 如果是长回答，TTS 播报了截断版本，Last Summary 包含完整内容

## 5. 检查日志

```bash
cat agent_state/app_events.jsonl | tail -5
cat agent_state/sessions.jsonl | tail -1 | python3 -m json.tool
cat agent_state/last_result.json
```

### 验收标准
- [ ] app_events.jsonl 包含 trigger / record_start / record_stop / stt_start / stt_done / claude_start / claude_done 等事件
- [ ] sessions.jsonl 记录中包含 transcript、summary、exit_code
- [ ] last_result.json 包含 prompt 和 summary

## 6. 多轮稳定性

1. 连续点击 `Trigger Recording` 3 次（每次讲不同的话）
2. 尝试快速双击

### 验收标准
- [ ] 每一轮都正常完成
- [ ] 快速点击不会创建多个录音线程
- [ ] 菜单状态每次都能恢复为 `Trigger Recording`

## 7. Health Check

点击 `Health Check`。

### 验收标准
- [ ] 7 项检查至少 5 项 PASS
- [ ] 每项 FAIL 都有对应的 fix hint
- [ ] Overall 显示整体状态

## 8. 退出

点击 `Quit`。

### 验收标准
- [ ] 菜单栏图标消失
- [ ] 进程退出，终端恢复

## 9. 构建 .app (可选)

```bash
./init.sh build-app
./init.sh install-app
open ~/Applications/VoiceClaudeAgent.app
```

### 验收标准
- [ ] `dist/VoiceClaudeAgent.app` 存在
- [ ] `~/Applications/VoiceClaudeAgent.app` 已安装
- [ ] 双击 .app 启动后菜单栏出现 🎤

## 故障排查

| 症状 | 可能原因 | 操作 |
|------|----------|------|
| `voice-claude-agent` not found | 未 source .venv | `source .venv/bin/activate` |
| Health Check: Microphone FAIL | TCC 未授权 | System Settings > Privacy > Microphone |
| Health Check: whisper-cli FAIL | 未安装 whisper.cpp | `brew install whisper-cpp` |
| Health Check: Whisper model FAIL | 模型路径未设置 | `export WHISPER_CPP_MODEL=/path/to/ggml-base.bin` |
| Trigger Recording: No Audio | 麦克风权限或设备问题 | 检查系统麦克风权限，运行 Mic Diagnostic |
| TTS 无声音 | macOS say 不可用 | 检查系统音量，确保未静音 |
