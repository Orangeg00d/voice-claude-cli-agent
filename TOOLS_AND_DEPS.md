# 其他工具与依赖一览

| 工具 / 依赖 | 分类 | 用途说明 |
|-------------|------|---------|
| `rumps` | Python 依赖 | macOS 菜单栏 App 框架，本项目菜单栏交互基础 |
| `pyobjc-core` + `pyobjc-framework-Cocoa` | Python 依赖 | rumps 的底层依赖，提供 Python ↔ AppKit/NSObject/Cocoa 桥接 |
| `sounddevice` + `numpy` | Python 依赖 | 麦克风录音与 PCM 数据处理（底层依赖 PortAudio） |
| `click` | Python 依赖 | CLI 命令行框架（`voice-claude-agent` 各级子命令） |
| `pytest` | Dev 依赖 | Python 测试框架 |
| `ruff` | Dev 依赖 | Python linter + formatter |
| `py2app` | Dev 依赖 | Python → macOS .app bundle 构建工具 |
| `whisper.cpp` | 外部依赖 | 本地离线语音转文字引擎，brew install whisper-cpp 安装 |
| `claude` | 外部依赖 | Anthropic Claude CLI，命令行 AI 代理，brew install claude 或等效 |
| macOS `say` | 系统工具 | macOS 内建 TTS 引擎，无需安装 |
| `osascript` | 系统工具 | macOS AppleScript / JXA 执行器，Apple Speech dictation 检查中使用 |
| `sounddevice` PortAudio 动态库 | 系统 / py2app 构建相关 | 位于 `_sounddevice_data/portaudio-binaries/libportaudio.dylib`，py2app bundle 需特殊处理 |
| `tccutil` | 系统工具 | macOS TCC 权限管理，用于重置麦克风权限 |
| `launchctl` | 系统工具 | 管理 launch agent，用于设置 Finder 启动 `.app` 的 GUI 环境变量 |
| `plutil` | 系统工具 | 用于检查 `Info.plist` 是否正确包含 `NSMicrophoneUsageDescription` 和 `LSUIElement` |
| 繁体 → 简体映射表 (硬编码) | 内部实现 | `src/voice_claude_agent/stt.py` 中 `_T2S_MAP`，覆盖 ~70 个常用繁简对应字 |
| `PyObjCTools.AppHelper` | Python 运行时 | 主线程安全调度 rumps.alert 避免 NSWindow 跨线程错误（F061） |
