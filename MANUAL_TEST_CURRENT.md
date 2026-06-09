# Voice Claude Agent — Manual Acceptance Test (Phase 23)

本文档覆盖当前最新版本的完整人工验收场景。执行前确认：
- `./init.sh check` 通过
- `./init.sh test` 全部通过
- whisper.cpp 已安装（可选），WHISPER_CPP_MODEL 已设置（可选）
- Volcengine ASR / TTS 凭证已配置（可选）
- 如果从 .app 启动，`launchctl setenv` 已配置 GUI 环境变量

## 1. Health Check

点击菜单栏 `Health Check`。

### 预期结果
- [ ] 各检查项显示 PASS 或 FAIL，FAIL 项附带修复提示
- [ ] Claude CLI、whisper-cli、Whisper model、Microphone、PortAudio、Agent state dir 均显示状态
- [ ] macOS say (TTS)、Record duration、Config file、STT backend、TTS backend 均显示状态
- [ ] Overall 行显示 ALL CHECKS PASSED 或 SOME CHECKS FAILED
- [ ] 若配置了 Volcengine ASR/TTS，相关项显示 CONFIGURED

### 常见失败排查
- Microphone FAIL：System Settings > Privacy & Security > Microphone 中授权
- PortAudio FAIL：重新运行 `python setup.py py2app` 重建 .app
- whisper-cli FAIL：`brew install whisper-cpp`
- Whisper model FAIL：下载 GGML 模型并设置 `WHISPER_CPP_MODEL`

## 2. Trigger Recording（完整语音交互）

点击菜单栏 `Trigger Recording`，对麦克风说话。

### 预期结果
- [ ] Current Status 变为 `Recording`
- [ ] 录制结束后 Current Status 变为 `Transcribing`
- [ ] STT 转写完成后 Current Status 变为 `Claude running`
- [ ] Claude 执行完成后听到语音播报
- [ ] 语音播报开始前 Current Status 短暂显示为 `Speaking`（通过 on_tts_start 回调）
- [ ] 流程结束后 Current Status 恢复 `Idle`
- [ ] Trigger Recording 菜单项恢复可点击
- [ ] Last Transcript 显示刚刚说的内容
- [ ] Last Summary 显示 Claude 回复摘要
- [ ] View Logs 可看到完整的 app_events 记录

### 常见失败排查
- No Audio：检查麦克风权限，确认 Mic Diagnostic 显示 ACCESSIBLE
- STT Error：尝试切换 STT backend（Settings 或 Reload Config）
- 无声音：确认 macOS say 可用（`./init.sh check`）
- 录制卡住：点击 Stop Current Run，然后重新 Trigger Recording

## 3. Volcengine ASR（Doubao 云端语音识别）

在 Settings 或 `~/.voice-claude-agent/config.json` 中设置：
- `VOICE_STT_BACKEND=volcengine-doubao`
- `VOLCENGINE_ASR_API_KEY=<your-api-key>`
（或 `VOLCENGINE_ASR_APP_ID` + `VOLCENGINE_ASR_ACCESS_TOKEN`）

点击 `Reload Config`，然后点击 `Trigger Recording`。

### 预期结果
- [ ] STT 转写使用 Volcengine/Doubao 云端服务
- [ ] transcript 为中文识别结果
- [ ] Health Check 中 Volcengine ASR 显示 CONFIGURED
- [ ] View Logs 中 stt_backend 为 `volcengine-doubao`

### 常见失败排查
- 凭证未设置：Settings 中确认 VOLCENGINE_ASR_API_KEY 已填写
- 网络问题：确认可访问 `openspeech.bytedance.com`
- Resource ID 错误：确认 VOLCENGINE_ASR_RESOURCE_ID 与控制台一致

## 4. Volcengine TTS（Doubao 云端语音合成）

在 Settings 或 `~/.voice-claude-agent/config.json` 中设置：
- `VOICE_TTS_BACKEND=volcengine-doubao`
- `VOLCENGINE_TTS_API_KEY=<your-api-key>`

点击 `Reload Config`，然后点击 `Preview TTS Voice`。

### 预期结果
- [ ] TTS 使用 Volcengine/Doubao 云端语音合成
- [ ] Preview TTS Voice 播放所选语音
- [ ] TTS Voice 菜单可切换不同语音（爽快思思、清润男声、东方浩然 等）
- [ ] 切换语音后 Preview TTS Voice 立即生效
- [ ] Health Check 中 TTS backend 显示 `volcengine-doubao (configured)`

### 常见失败排查
- API Key 缺失：Settings 中确认 VOLCENGINE_TTS_API_KEY 已填写
- 凭证问题导致 fallback：View Logs 中查看 `tts_fallback_reason`
- Resource ID 不匹配：切换到 moon_bigtts 语音（爽快思思/清润男声/VV）
- 实验性 BV 语音需要匹配的 Resource ID

## 5. Preview TTS Voice

点击菜单栏 `Preview TTS Voice`。

### 预期结果
- [ ] 弹出输入框，默认显示"你好，我是语音助手……"测试文本
- [ ] 点击 Cancel 不播放音频，不写 session
- [ ] 点击 OK 后播放 TTS
- [ ] 菜单标题变为 `Previewing...` 然后恢复 `Preview TTS Voice`
- [ ] 不调用 Claude CLI，不写 session 日志
- [ ] Volcengine 失败时自动 fallback 到 macOS say

## 6. Stop Current Run

在 Trigger Recording 运行期间（Recording/Transcribing/Claude running/Speaking 阶段）点击 `Stop Current Run`。

### 预期结果
- [ ] 在 Recording/Transcribing/Claude running 阶段：Claude CLI 子进程被取消，`cycle_cancelled` 事件写入
- [ ] 在 Speaking 阶段：TTS 播放被停止（say/afplay 进程被 kill），`tts_cancelled` 事件写入
- [ ] Current Status 变为 `Cancelled`
- [ ] 空闲状态下点击：弹出"No active run is currently executing."，不写事件

## 7. View Logs

点击菜单栏 `View Logs`。

### 预期结果
- [ ] 显示当前 STT/TTS 配置摘要
- [ ] 显示最近 10 条 app_events（含时间戳和事件名）
- [ ] 显示 last_result（prompt、exit_code、summary、cancelled、tts_cancelled 等）
- [ ] 若存在 TTS resource mismatch，显示资源不匹配诊断提示
- [ ] 若 Claude CLI 正在运行，显示运行时长
- [ ] 无日志时显示占位消息

## 8. Reload Config

修改 `~/.voice-claude-agent/config.json`（或在 Settings 中修改并保存），然后点击 `Reload Config`。

### 预期结果
- [ ] 弹出"Config Reloaded"确认弹窗
- [ ] record_seconds、STT backend、TTS voice 立即更新
- [ ] Health Check 反映新配置
- [ ] 配置文件不存在时仍可正常 reload（使用默认值）
- [ ] 配置文件 JSON 格式错误时显示"Reload Config Failed"错误提示，不崩溃

## 9. 多轮稳定性

重复执行 Trigger Recording 3-5 次。

### 预期结果
- [ ] 每次录制独立，不相互影响
- [ ] 快速连续点击 3 次 Trigger Recording，只有一次实际执行
- [ ] Current Status 每次正确恢复 Idle
- [ ] app_events 日志无重复/交叉的 cycle 事件
- [ ] sessions.jsonl 每轮一条记录

## 10. App 退出

点击 `Quit`。

### 预期结果
- [ ] 如果 wake loop 正在运行，先停止再退出
- [ ] App 进程正常退出，无残留进程
