# Voice Claude Agent — 手动验收手册 (Phase 8)

本文档记录菜单栏 App 的真实手动验收步骤，覆盖日常使用中最常见的操作和故障排查。

## 前置条件

1. 已安装 whisper.cpp：`brew install whisper-cpp`
2. 已下载 GGML 模型并设置环境变量：
   ```bash
   export WHISPER_CPP_MODEL=/path/to/ggml-base.bin
   ```
   如果从 Finder 双击 `.app` 启动，macOS 不一定继承终端里的 `export`。请先写入 GUI 会话环境：
   ```bash
   launchctl setenv WHISPER_CPP_MODEL /path/to/ggml-base.bin
   launchctl setenv WHISPER_CPP_LANGUAGE zh
   ```
   然后重新打开 `dist/VoiceClaudeAgent.app`。
3. 已安装项目依赖：`./init.sh install`
4. 已运行测试：`./init.sh test`（167/167 通过）
5. 如需 .app 构建：`python setup.py py2app`

## 1. 启动菜单栏 App

### 终端模式
```bash
voice-claude-agent app --stt-backend whisper-cli
```

预期：
- 菜单栏出现 🎤 图标
- 菜单包含：Start Wake、Stop Wake、Trigger Recording、Mic Diagnostic、Mic Status、Last Transcript、Last Summary、Quit

### .app 模式
```bash
open dist/VoiceClaudeAgent.app
```

预期：
- 同样出现菜单栏 App
- 如果麦克风权限被拒绝，会弹出诊断对话框

## 2. Trigger Recording（单次语音 → Claude → TTS）

### 步骤
1. 点击 `Trigger Recording`
2. 观察状态变化：`Recording...` → `Transcribing...` → `Running Claude...` → `Done ✓`
3. 说话直到录音结束（默认 5 秒）

### 验收标准
- [ ] 菜单状态依次变化，不卡住
- [ ] TTS 播报了 Claude 的回答
- [ ] `Last Transcript` 显示了识别的文本
- [ ] `Last Summary` 显示了完整的回答摘要
- [ ] 如果回答很长，TTS 播报了截断版本 + "完整内容可在菜单栏 Last Summary 查看"
- [ ] 1.5 秒后状态恢复为 `Trigger Recording`

### 故障排查
- 如果状态卡在 `Recording...` → 点击 `Mic Diagnostic` 检查设备
- 如果弹窗 `No Audio` → 检查麦克风权限（System Settings > Privacy & Security > Microphone）
- 如果弹窗 `STT Error` → 检查 `WHISPER_CPP_MODEL` 是否设置正确

## 3. Mic Diagnostic

### 步骤
1. 点击 `Mic Diagnostic`
2. 阅读弹出窗口

### 验收标准
- [ ] 显示 Bundle ID: com.voiceclaude.agent
- [ ] 显示 Python 版本
- [ ] 显示 Default input device（如 "MacBook Pro麦克风"）
- [ ] 显示 PortAudio loaded: YES
- [ ] 显示 PortAudio library 路径
- [ ] 显示 Agent state dir 路径

### 故障排查
- 如果 PortAudio loaded: NO → 可能是 py2app 未正确提取 dylib，重新运行 `python setup.py py2app`
- 如果 input device 为 unknown → 检查系统是否识别麦克风

## 4. TCC / 麦克风权限

### 步骤
1. 打开 System Settings > Privacy & Security > Microphone
2. 检查 `Voice Claude Agent` 或 `com.voiceclaude.agent` 是否在列表中

### 如果不在列表中
```bash
# 重置 TCC 权限
tccutil reset Microphone com.voiceclaude.agent

# 重新打开 App（必须在 Finder 中双击，不是终端）
open dist/VoiceClaudeAgent.app
```

### 验收标准
- [ ] App 出现在麦克风权限列表中
- [ ] 切换权限开关后，Mic Status 相应变化

## 5. whisper-cli 验证

### 步骤
1. 终端测试 whisper-cli 独立工作：
   ```bash
   whisper-cli -m $WHISPER_CPP_MODEL -f /path/to/test.wav -nt --no-timestamps
   ```
2. 在菜单栏 App 中 Trigger Recording

### 验收标准
- [ ] whisper-cli 终端命令返回转写文本
- [ ] 菜单栏 Last Transcript 显示了正确的转写结果

## 6. 多轮稳定性

### 步骤
1. 连续点击 `Trigger Recording` 3 次（快速点击）
2. 等待一轮完成后再次点击
3. 多次正常触发

### 验收标准
- [ ] 快速点击不会创建多个录音线程
- [ ] 菜单状态不会卡住
- [ ] 每次都能正常恢复为 `Trigger Recording`
- [ ] 每次都能正确播放 TTS

## 7. 日志检查

### 步骤
1. 查看 app-events 日志：
   ```bash
   cat agent_state/app_events.jsonl | tail -20
   ```
2. 查看会话日志：
   ```bash
   cat agent_state/sessions.jsonl | tail -5
   ```
3. 查看最新结果：
   ```bash
   cat agent_state/last_result.json
   ```

### 验收标准
- [ ] app_events.jsonl 包含 trigger / record_start / record_stop / stt_start / stt_done / claude_start / claude_done / tts_done / cycle_done
- [ ] sessions.jsonl 每行是合法 JSON
- [ ] last_result.json 包含 prompt / exit_code / summary

## 8. 已知问题与限制

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| App 不出现在麦克风权限列表 | TCC 不注册 CLI/bundle | `tccutil reset` + Finder 双击打开 |
| STT 返回空或错误 | whisper-cli 或模型问题 | 检查 `WHISPER_CPP_MODEL`，终端测试 whisper-cli；Finder 启动时用 `launchctl setenv` 写入 GUI 环境 |
| 录音返回空音频 | 麦克风权限或设备问题 | 运行 Mic Diagnostic，检查系统设置 |
| 大回答 TTS 朗读太长 | Summary 超出语音合理长度 | 已自动截断，完整内容在 Last Summary |
| py2app bundle 闪退 | PortAudio 动态库问题 | 重新 `python setup.py py2app` |
