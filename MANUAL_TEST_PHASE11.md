# Voice Claude Agent — Final Acceptance Test (Phase 11)

本文档覆盖最终人工验收的 6 个核心场景。执行前确认：
- `./init.sh check` 通过
- `./init.sh test` 223 项全部通过
- whisper.cpp 已安装，WHISPER_CPP_MODEL 已设置
- 如果从 .app 启动，`launchctl setenv` 已配置 GUI 环境变量

## 1. Health Check

点击菜单栏 `Health Check`。

### 验收标准
- [ ] 8 项检查至少有 6 项 PASS
- [ ] Claude CLI — PASS 或明确的 install 提示
- [ ] whisper-cli — PASS 或 brew install 提示
- [ ] Whisper model — PASS 或 WHISPER_CPP_MODEL 提示
- [ ] Microphone — PASS 或 System Settings 路径
- [ ] PortAudio — PASS 或 "rebuild with python setup.py py2app" 提示
- [ ] Agent state dir — PASS
- [ ] macOS say (TTS) — PASS
- [ ] Record duration — 显示当前录音秒数
- [ ] Overall 显示 ALL CHECKS PASSED（或明确列出失败项）

## 2. Mic Diagnostic

点击菜单栏 `Mic Diagnostic`。

### 验收标准
- [ ] 显示 Bundle ID: com.voiceclaude.agent
- [ ] 显示 Python 版本
- [ ] 显示 Default input device 名称（如 "MacBook Pro麦克风"）
- [ ] 显示 PortAudio loaded: YES
- [ ] 显示 Agent state dir 路径
- [ ] 显示 TCC troubleshooting 提示（tccutil / NSMicrophoneUsageDescription）

## 3. 普通语音指令

1. 点击 `Trigger Recording`
2. 说一句常规话，如 "请用一句话介绍你自己"
3. 等待 `Recording...` → `Transcribing...` → `Running Claude...` → `Done ✓`
4. 听 TTS 播报

### 验收标准
- [ ] 状态栏依次变化
- [ ] TTS 播报了 Claude 的回答
- [ ] Last Transcript 显示了正确转写结果
- [ ] Last Summary 显示了完整的 Claude 回答
- [ ] 没有触发语音确认（因为是普通指令，不是高风险）

## 4. 高风险语音指令 + 拒绝

1. 点击 `Trigger Recording`
2. 说一句高风险指令，如 "请帮我 git push origin main"
3. 听到 TTS 问 "检测到高风险动作...请说同意以继续，或说取消以拒绝"
4. 状态显示 `Confirm? Say 同意 or 取消...`
5. 对着麦克风说 "取消"
6. 观察结果

### 验收标准
- [ ] TTS 播报了确认提示
- [ ] 第二段录音被转录为 "取消" 或类似拒绝词
- [ ] TTS 播报 "高风险动作已被拒绝，未执行"
- [ ] Claude CLI 没有被调用
- [ ] 菜单状态恢复为 `Trigger Recording`

## 5. 高风险语音指令 + 同意

1. 点击 `Trigger Recording`
2. 说一句高风险指令，如 "请帮我删除临时文件"
3. 听到确认提示
4. 对着麦克风说 "同意" 或 "继续"
5. 观察结果

### 验收标准
- [ ] TTS 播报了确认提示
- [ ] 第二段录音被识别为同意
- [ ] Claude CLI 被执行
- [ ] TTS 播报了 Claude 的回答
- [ ] Last Summary 更新了

## 6. View Logs 检查

1. 在完成上述所有步骤后，点击 `View Logs`

### 验收标准
- [ ] 显示 "Recent app-events (N of M total):" 统计
- [ ] 包含 `trigger`、`record_start`、`record_stop` 等事件
- [ ] 对于高风险步骤，包含 `risk_high_confirm_start`、`risk_high_confirm_*` 事件
- [ ] 显示 "Last result:" 含 prompt / exit_code / summary
- [ ] 事件时间戳合理

## 故障排查

| 症状 | 操作 |
|------|------|
| Health Check: Microphone FAIL | System Settings > Privacy > Microphone |
| Health Check: whisper-cli FAIL | `brew install whisper-cpp` |
| 录音无音频 | 检查麦克风权限，运行 Mic Diagnostic |
| TTS 无声音 | 检查系统音量 |
| 语音确认识别为 unclear | 说话更清晰、减少环境噪音 |
