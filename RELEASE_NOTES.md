# RELEASE NOTES — Voice Claude Agent v0.1.0

## 概述

Voice Claude Agent 是一个 macOS local-first 语音 Agent。用户通过菜单栏点击触发录音，语音经 whisper.cpp 或可选 Volcengine/Doubao ASR 转文字后交给 Claude CLI 执行，结果通过 macOS `say` 或可选 Volcengine/Doubao TTS 播报。

## 版本信息

- **版本**: 0.1.0
- **平台**: macOS (Apple Silicon / Intel)
- **Python**: 3.11+
- **构建日期**: 2026-06-07

## 当前 main 状态

- **验收项**: 74/74 passed (F001-F074)
- **测试数量**: 298
- **最新阶段**: Phase 20 — runtime backend logs

## v0.1.0 功能清单 (67 项验收全部通过)

### 核心链路 (F001-F012)
- Claude CLI 可用性检查
- 文本指令 → Claude CLI → 结果记录 → 摘要 → TTS 播报
- 风险三级分类 (只读 / 可恢复 / 破坏性)
- 高风险动作二次确认
- JSONL 会话日志 + last_result
- STT 接口 + 唤醒入口接口

### 录音与麦克风 (F013-F021)
- SoundDevice 真实录音 + FakeRecorder 测试
- demo-voice CLI 管道
- 麦克风权限检查
- Apple Speech 听写后端 (状态/错误报告)
- 录音失败的用户提示

### 唤醒循环 (F016-F018)
- wake 命令：按键唤醒 → 录音 → STT → Claude → TTS 循环
- ManualWakeTrigger 集成
- Fake 模式可测

### CLI 打包与依赖 (F022-F023)
- `./init.sh install` 可编辑安装
- `voice-claude-agent --help` 列出全部命令

### UX 防御 (F024-F031)
- 每轮状态输出 (waiting / woke / recording / transcript / summary)
- 空音频 / 空 transcript / STT 错误不崩溃
- Claude 超时写 session + TTS 提醒
- 录音 start/stop 异常保护
- whisper-cli 二进制验证 + Python whisper 拒绝 + 模型配置
- Apple Speech 听写状态检测
- README 文档

### 菜单栏 App (F032-F042)
- rumps 菜单栏 App
- Start/Stop Wake 控制 + Mic Status + Trigger Recording
- STT 后端透传 (whisper-cli / text-input / apple-speech)
- py2app .app 构建 + PortAudio dylib 提取
- 麦克风权限拒绝 UX (原生对话框 + Mic Status 更新)
- 菜单栏 / CLI 日志一致性
- Mic Diagnostic 菜单项
- 非交互式录音 (timer 替代 input())
- 默认 whisper-cli 后端

### 稳定性 (F043-F048)
- Trigger Recording 非重入保护
- 结构化 app-events 日志 (9 种事件类型)
- 所有错误路径菜单标题恢复
- Last Transcript / Last Summary 菜单查看
- TTS 长回答截断 + 代码块去除
- 并发触发安全

### 文档与发布 (F049-F055)
- README 手动验收步骤
- MANUAL_TEST_PHASE8.md
- View Logs 菜单项
- VOICE_RECORD_SECONDS 配置
- 可行动的错误提示审计
- RELEASE_NOTES.md + PRIVACY.md

### 发布硬化 (F056-F060)
- README v0.1.0 状态清理
- `./init.sh build-app` / `./init.sh install-app`
- Health Check 菜单项
- `~/.voice-claude-agent/config.json` 本地配置
- MANUAL_TEST_RELEASE.md 发布验收手册

### 真实使用修正 (F061-F064)
- 后台线程 alert 主线程调度，避免 NSWindow 跨线程崩溃
- 简体中文输出约束与繁转简兜底
- Health Check / Mic Diagnostic 显示当前录音时长
- 本地配置文件录音时长文档
- Trigger Recording 连续点击保护，确保同一时间只运行一个录音执行周期
- 高风险动作菜单栏语音确认：说“同意/确认/继续”执行，说“取消/不要/拒绝”中止

### 发布就绪审计 (F065)
- DEVELOPER_PROGRAM_APPLICATION.md 开发者计划申请材料
- MANUAL_TEST_PHASE11.md 最终人工验收手册
- README / RELEASE_NOTES / PRIVACY / 手工验收文档一致性审计

### 仓库元数据 (F066)
- MIT License
- SECURITY.md 安全报告与无遥测声明
- CONTRIBUTING.md Claude + Codex 协同开发流程
- TOOLS_AND_DEPS.md 依赖和工具索引

### GitHub 发布展示 (F067)
- README 顶部 badges：MIT、macOS、Python 3.11+、223 tests
- GITHUB_RELEASE_DRAFT.md v0.1.0 发布草稿
- REPOSITORY_METADATA.md GitHub description、topics、release checklist 建议

### Unreleased: Settings UI (F068)
- 菜单栏新增 Settings... 与 Reset Settings
- Settings 支持查看/编辑 `VOICE_RECORD_SECONDS`、`VOICE_STT_BACKEND`、`WHISPER_CPP_MODEL`、`WHISPER_CPP_LANGUAGE`
- 保存后运行中的 App 立即应用 `record_seconds` 和 `stt_backend`
- Settings 保存使用原子写入，空输入或无有效 key 不会覆盖/清空 config.json
- Health Check / Mic Diagnostic 显示配置路径和当前后端
- 录音 stop/close 超时时会抢救已捕获音频继续处理，减少 `No Audio` 误报

### Unreleased: Volcengine/Doubao ASR (F069)
- 新增 `VOICE_STT_BACKEND=volcengine-doubao`
- 使用官方 BigModel ASR Flash v3 endpoint
- 支持 `VOLCENGINE_ASR_API_KEY` 以及旧版 App ID + Access Token
- Settings UI、Health Check、Mic Diagnostic、CLI check 均脱敏展示密钥
- 新增 `docs/VOLCENGINE_ASR_SETUP.md`

### Unreleased: Claude Workdir Pinning (F070)
- 新增 `VOICE_CLAUDE_WORKDIR`
- `claude -p` subprocess 调用固定在配置的项目目录执行
- 无效目录会阻止执行并返回明确错误
- Settings UI、Health Check、Mic Diagnostic、View Logs 显示 Claude workdir

### Unreleased: Volcengine/Doubao TTS (F071)
- 新增 `VOICE_TTS_BACKEND=volcengine-doubao`
- 使用 Volcengine/Doubao TTS HTTP Chunked V3 单向流式接口
- 支持 `VOLCENGINE_TTS_API_KEY`、Resource ID、Voice Type、Audio Format、Endpoint 配置
- TTS 失败自动 fallback 到 macOS `say`
- sessions、last_result、View Logs、app_events 记录 `tts_backend`、`tts_duration_seconds`、`tts_fallback_used`
- 新增 `docs/VOLCENGINE_TTS_SETUP.md`

### Unreleased: TTS Voice Preview (F072)
- 菜单栏新增 `Preview TTS Voice`
- 可输入测试文本并直接使用当前 TTS backend 播放
- 不调用 Claude CLI，不写 session
- 记录 `tts_preview_start`、`tts_preview_failed`、`tts_preview_done` app_events
- Volcengine TTS fallback 会在预览日志中显式标记

### Unreleased: TTS Voice Selector (F073)
- 菜单栏新增 `TTS Voice` 子菜单
- 支持爽快思思、清润男声、标准女声、标准男声、VV 女声（方言）
- 选择 `moon_bigtts` 系列时自动设置 `VOLCENGINE_TTS_RESOURCE_ID=seed-tts-1.0`
- 选择 BV 系列时保留已有 Resource ID，并提示用户确认匹配关系
- Health Check / Mic Diagnostic 显示当前 TTS voice type

### Unreleased: Runtime Backend Logs (F074)
- View Logs 顶部显示当前 STT/TTS 配置
- Last result 显示当轮实际 `stt_backend`、`tts_backend`、`tts_voice_type`、`tts_resource_id`
- app_events 的 `tts_done` 显示 STT/TTS backend、音色和 fallback 状态
- sessions / last_result 记录 STT backend、TTS voice type、TTS resource ID

## 测试

- **测试框架**: pytest
- **当前测试数量**: 298
- **v0.1.0 发布测试数量**: 223
- **测试覆盖**: CLI 管道、录音、STT、风险分类、菜单栏生命周期、并发安全、日志格式

## 依赖

| 依赖 | 用途 |
|------|------|
| click | CLI 命令行 |
| sounddevice + numpy | 麦克风录音 |
| rumps | macOS 菜单栏 App |
| pytest + ruff | 测试 + 代码检查 |
| py2app | .app 构建 (dev) |

### 可选外部依赖

| 依赖 | 用途 |
|------|------|
| whisper.cpp (brew) | 本地语音转文字 |
| GGML 模型 | whisper.cpp 推理模型 |

## 已知限制

- **非语音唤醒词**: 触发方式为手动点击菜单栏按钮，无真实唤醒词检测
- **TCC 权限**: py2app 构建的 .app 可能需要 Finder 双击启动才能触发 macOS 麦克风权限对话框
- **Python 3.14 兼容**: py2app 在 setuptools ≥ 82 上需要 monkeypatch

## Changelog

### v0.1.0 (2026-06-07)
- Initial release with complete Phase 1-13 feature set
- 223 passing tests
- Standalone .app build support via py2app
