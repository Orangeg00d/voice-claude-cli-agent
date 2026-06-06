# 语音唤醒 Claude CLI Agent 开发计划书

本文档用于指导 Claude Desktop 或 Claude CLI 开发一个最小可用的本地语音 Agent。该 Agent 的目标是：通过语音唤醒接收开发指令，调用本地 Claude CLI 执行任务，并在任务完成后用语音播报执行结果。

本文档采用 Anthropic《Effective harnesses for long-running agents》中描述的 long-running agent harness 思路：先由 initializer 建立可持续工作的项目环境，再由后续 coding agent 每次只完成一个增量任务，且每轮都要留下可验证、可接续、可回滚的状态记录。

参考文章：
https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents

## 0. 当前已确定需求

必须实现：

- macOS 本地运行。
- 支持语音唤醒。
- 支持将用户语音转成文字指令。
- 支持调用本地 Claude CLI。
- 支持等待 Claude CLI 执行完成。
- 支持总结 Claude CLI 执行结果。
- 支持语音播报执行结果。
- 支持高风险动作前二次确认。
- 支持自检、日志、测试和验收。

第一版只操纵 Claude CLI，不操纵 Claude Desktop、Codex App 或浏览器 UI。

### 0.1 术语说明

- STT：Speech-to-Text，意思是“语音转文字”。用户说话以后，系统需要先把音频转换成文字指令，再交给 Claude CLI。
- TTS：Text-to-Speech，意思是“文字转语音”。Claude CLI 执行完成后，系统把结果摘要读出来。
- 唤醒词：类似“嘿 Siri”或“小爱同学”的触发词。系统常驻监听麦克风，但只有听到指定唤醒词后才开始录制真正的用户指令。
- Porcupine：一个常见的本地唤醒词检测引擎，适合做“听到某个词才唤醒”的功能。
- openWakeWord：另一个开源唤醒词检测方案，也用于本地识别唤醒词。

### 0.2 已确认产品方向

- 最终形态要做成 macOS 常驻 App。
- 不需要开机自动启动，用户手动打开 App 即可。
- 允许 App 在运行期间常驻监听麦克风。
- 第一阶段先做“按键/命令行唤醒”，不接真实唤醒词。
- 按键唤醒模式稳定后，再进化到“语音唤醒词”。
- Phase 1 优先跑通文本触发 MVP：文本指令 -> Claude CLI -> 结果记录 -> 语音播报。

## 1. 默认技术架构

除非用户另行指定，按以下架构实现 MVP：

```text
macOS 常驻进程
  -> 按键/命令行触发
  -> 后续可替换为唤醒词检测
  -> 录音
  -> Speech-to-Text
  -> 指令路由
  -> Claude CLI 执行器
  -> 结果解析与摘要
  -> Text-to-Speech 播报
  -> 日志与任务状态文件
```

推荐第一版技术栈：

- 语言：Python 3.11+。
- 包管理：uv 或 pip，优先使用项目现有工具；若无现有工具，使用 `uv`。
- 唤醒入口：优先设计可插拔接口，MVP 先提供快捷键/命令行触发，随后接入本地唤醒词。
- 录音：macOS 麦克风权限 + Python 音频库。
- STT：优先可配置，MVP 先做接口和 mock；真实语音转文字后续再选择 OpenAI Whisper API、本地 whisper.cpp 或 Apple Speech。
- TTS：MVP 使用 macOS `say` 命令。
- Claude CLI：通过 subprocess 调用本地 `claude -p`。
- 状态存储：本地 JSONL 日志 + `agent-progress.md` + `feature_list.json`。
- 测试：pytest。

说明：

- 为了降低第一版复杂度，唤醒词检测和 STT/TTS 都必须抽象成接口。
- 如果唤醒词库、STT 凭据或麦克风权限暂不可用，项目仍应能通过命令行文本输入模式完整跑通。

## 2. Harness 文件约定

initializer agent 必须创建以下文件：

```text
.
├── README.md
├── CLAUDE.md
├── CODEX_REVIEW_GUIDE.md
├── init.sh
├── feature_list.json
├── agent-progress.md
├── agent_state/
│   ├── sessions.jsonl
│   └── last_result.json
├── src/
│   └── voice_claude_agent/
├── tests/
└── pyproject.toml
```

### 2.1 init.sh

`init.sh` 是每个 coding agent 开始工作前必须运行或阅读的入口脚本。

必须支持：

```bash
./init.sh check
./init.sh test
./init.sh demo-text
```

建议支持：

```bash
./init.sh install
./init.sh lint
./init.sh format
```

要求：

- `check` 检查 Python、依赖、Claude CLI、macOS `say` 是否可用。
- `test` 运行全部自动化测试。
- `demo-text` 不依赖麦克风，使用文本指令模拟完整流程。

### 2.2 feature_list.json

`feature_list.json` 是唯一的功能验收清单。coding agent 只能在通过验证后修改对应 feature 的 `passes` 字段，不得删除、改写或弱化测试步骤。

格式：

```json
{
  "features": [
    {
      "id": "F001",
      "priority": 1,
      "category": "core",
      "description": "Agent can check local Claude CLI availability",
      "steps": [
        "Run init check",
        "Verify command -v claude succeeds or reports actionable error",
        "Verify claude --version is captured when available"
      ],
      "passes": false,
      "evidence": ""
    }
  ]
}
```

初始 feature 至少包括：

- F001：检查 Claude CLI 可用性。
- F002：文本指令模式可以调用 Claude CLI。
- F003：Claude CLI stdout/stderr/exit code 被完整记录。
- F004：执行结束后生成结构化摘要。
- F005：TTS 可以播报摘要，MVP 可使用 `say`。
- F006：高风险动作会进入确认流程。
- F007：拒绝执行未确认的 push、delete、deploy 等动作。
- F008：任务日志写入 JSONL。
- F009：失败时播报失败原因和下一步建议。
- F010：提供语音输入接口，若真实 STT 不可用则提供 mock。
- F011：提供唤醒入口接口，若真实 wake word 不可用则提供快捷键/命令行替代。
- F012：完整 demo-text 流程可自动化测试。

### 2.3 agent-progress.md

`agent-progress.md` 是跨会话进度记录。每个 coding agent 结束前必须追加记录：

```markdown
## YYYY-MM-DD HH:mm - <session title>

### Completed
- ...

### Verification
- `...` passed
- `...` failed: <reason>

### Files Changed
- ...

### Next Recommended Task
- ...

### Risks / Notes
- ...
```

### 2.4 agent_state/sessions.jsonl

每次执行用户命令都记录一行 JSON：

```json
{
  "timestamp": "2026-06-06T12:00:00+08:00",
  "input_mode": "text|voice|mock",
  "transcript": "让 Claude 检查当前仓库状态",
  "classified_intent": "run_claude_task",
  "risk_level": "read_only|recoverable|external_or_destructive",
  "confirmation_required": false,
  "confirmation_received": false,
  "claude_command": ["claude", "-p", "..."],
  "exit_code": 0,
  "summary": "...",
  "spoken": true
}
```

## 3. Agent 行为边界

### 3.1 风险等级

只读动作：

- 查看 git 状态。
- 查看文件。
- 读取 GitHub 任务。
- 让 Claude CLI 总结代码。

可恢复动作：

- 修改本地代码。
- 创建分支。
- 运行测试。
- 安装开发依赖。

外部可见或破坏性动作：

- `git push`
- 创建/合并 PR。
- 删除文件或目录。
- 修改生产配置。
- 部署。
- 改写 git 历史。

规则：

- 只读动作可直接执行。
- 可恢复动作要记录日志。
- 外部可见或破坏性动作必须二次确认。
- 未确认时必须拒绝执行，并语音说明原因。

### 3.2 Claude CLI 调用规则

Claude CLI 只能通过受控执行器调用。

默认命令：

```bash
claude -p "<prompt>"
```

执行器必须：

- 捕获 stdout。
- 捕获 stderr。
- 捕获 exit code。
- 设置超时。
- 将完整结果写入日志。
- 将长输出压缩成用户可听懂的摘要。

禁止：

- 默认使用 `--dangerously-skip-permissions`。
- 默认让 Claude CLI 直接 push、deploy、删除数据。
- 在没有日志的情况下静默执行。

## 4. Initializer Agent 任务

Claude Desktop 第一次开发时，应使用 initializer prompt。

目标：

- 搭建项目骨架。
- 写入 harness 文件。
- 建立 feature list。
- 实现最小 demo-text 路径。
- 提交一次初始可运行状态。

Initializer 必须完成：

1. 创建 `pyproject.toml`。
2. 创建包目录 `src/voice_claude_agent/`。
3. 创建 `init.sh`。
4. 创建 `feature_list.json`。
5. 创建 `agent-progress.md`。
6. 创建 `agent_state/`。
7. 确认 `README.md`、`CLAUDE.md`、`CODEX_REVIEW_GUIDE.md` 已存在且与本计划一致。
8. 实现文本输入到 Claude CLI 的最小链路。
9. 实现 macOS `say` 播报。
10. 实现风险分类和确认占位。
11. 编写 pytest 测试。
12. 运行 `./init.sh check` 和 `./init.sh test`。
13. 写入进度记录。
14. 创建 git commit。

Initializer prompt：

```text
你是本项目的 initializer agent。请按照 VOICE_CLAUDE_CLI_AGENT_PLAN.md 建立一个可长期接续开发的项目环境。

重点要求：
- 不要试图一次性完成所有高级功能。
- 必须创建 init.sh、feature_list.json、agent-progress.md、agent_state/sessions.jsonl。
- 必须实现 demo-text 模式：用户输入一段文本，程序调用 claude -p，并用 macOS say 播报摘要。
- 必须实现风险分类的基础逻辑。
- 必须编写自动化测试，至少覆盖 Claude CLI 执行器、风险分类、日志写入、TTS mock。
- 必须运行验证命令。
- 最后必须追加 agent-progress.md，并提交 git commit。
```

## 5. Coding Agent 每轮工作流程

每个后续 coding agent 都必须遵循：

1. 运行：

   ```bash
   pwd
   git status
   git log --oneline -10
   ```

2. 阅读：

   ```text
   VOICE_CLAUDE_CLI_AGENT_PLAN.md
   CLAUDE.md
   CODEX_REVIEW_GUIDE.md
   agent-progress.md
   feature_list.json
   init.sh
   ```

3. 运行：

   ```bash
   ./init.sh check
   ./init.sh test
   ```

4. 从 `feature_list.json` 选择一个 `passes=false` 的最高优先级 feature。

5. 只完成这一个 feature。

6. 自测该 feature。

7. 只有通过验证后，才能把对应 feature 的 `passes` 改为 `true` 并写入 `evidence`。

8. 追加 `agent-progress.md`。

9. 保持 git 工作区干净，提交一个描述清晰的 commit。

Coding agent prompt：

```text
你是本项目的 coding agent。本轮只完成一个 feature。

请先阅读 VOICE_CLAUDE_CLI_AGENT_PLAN.md、agent-progress.md、feature_list.json 和 init.sh。
然后运行 ./init.sh check 与 ./init.sh test，确认当前状态。
从 feature_list.json 中选择最高优先级且 passes=false 的一个 feature。
只实现这个 feature，不扩大范围。

完成后：
1. 运行相关测试。
2. 只有验证通过后才更新 feature_list.json 中该 feature 的 passes 和 evidence。
3. 追加 agent-progress.md。
4. 提交 git commit。
5. 在最终回复中列出修改摘要、验证命令、commit hash 和剩余风险。
```

## 6. 推荐模块设计

```text
src/voice_claude_agent/
├── __init__.py
├── cli.py
├── config.py
├── claude_runner.py
├── command_router.py
├── risk.py
├── confirmation.py
├── recorder.py
├── stt.py
├── wake.py
├── tts.py
├── summarizer.py
└── logging_store.py
```

### 6.1 claude_runner.py

职责：

- 构造 Claude CLI 命令。
- 执行 subprocess。
- 处理超时。
- 返回结构化结果。

数据结构：

```python
@dataclass
class ClaudeRunResult:
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool
```

### 6.2 risk.py

职责：

- 根据用户文本和拟执行动作判断风险。
- 输出 `read_only`、`recoverable`、`external_or_destructive`。

必须覆盖关键词：

- push
- deploy
- delete
- remove
- rm -rf
- merge
- reset --hard
- production
- secret
- token

### 6.3 confirmation.py

职责：

- 对高风险动作要求确认。
- MVP 可使用命令行 `yes/no`。
- 后续可替换为语音确认。

### 6.4 tts.py

职责：

- 抽象 TTS 接口。
- MVP 实现 `MacOSSaySpeaker`。
- 测试使用 `FakeSpeaker`。

### 6.5 stt.py

职责：

- 抽象 STT 接口。
- MVP 可先实现 `TextInputTranscriber` 和 `FakeTranscriber`。
- 后续接入 Whisper 或本地模型。

### 6.6 wake.py

职责：

- 抽象唤醒入口。
- MVP 可先实现 `ManualWakeTrigger`。
- 后续接入真实 wake word。

## 7. MVP 命令行体验

必须支持：

```bash
voice-claude-agent check
voice-claude-agent demo-text "让 Claude 回复 OK"
voice-claude-agent run-text "请总结当前项目结构"
```

预期行为：

- `check`：检查依赖和 Claude CLI。
- `demo-text`：走完整链路，但适合测试。
- `run-text`：将文本传给 Claude CLI，记录结果并播报摘要。

## 8. 自检与测试要求

必须有自动化测试覆盖：

- Claude CLI 命令构造。
- Claude CLI 执行器对成功、失败、超时的处理。
- 风险分类。
- 高风险动作需要确认。
- 未确认高风险动作不执行。
- 日志 JSONL 写入。
- TTS speaker 可被 mock。
- demo-text 不依赖真实麦克风。

推荐测试命令：

```bash
./init.sh test
```

推荐 lint：

```bash
ruff check .
```

推荐格式化：

```bash
ruff format .
```

## 9. 验收标准

Codex 最终验收时检查：

- `./init.sh check` 通过，或失败信息明确可行动。
- `./init.sh test` 通过。
- `voice-claude-agent demo-text "请回复 OK"` 可以跑通。
- Claude CLI 输出被记录到 `agent_state/sessions.jsonl`。
- 执行摘要能被播报；测试环境可用 fake speaker 验证。
- 高风险命令不会在未确认时执行。
- `feature_list.json` 没有被删除、弱化或篡改测试步骤。
- `agent-progress.md` 有清晰进度记录。
- git history 中每轮 commit 小而清晰。
- 代码结构清楚，没有把所有逻辑塞进一个文件。

## 10. 开发阶段规划

### Phase 1：文本触发 MVP

目标：

- 不依赖真实语音。
- 文本输入触发 Claude CLI。
- 执行结果播报。
- 完成日志、自检和测试。
- 不接真实麦克风。
- 不接真实唤醒词。

完成标志：

- F001-F009 通过。

### Phase 2：语音输入抽象

目标：

- 增加 STT 接口。
- 支持真实录音或 mock 录音。
- 支持手动开始/结束录音。

完成标志：

- F010 通过。

### Phase 3：唤醒入口

目标：

- 增加 wake trigger 接口。
- 先实现按键或命令行模拟唤醒。
- 后续再接入本地唤醒词。

完成标志：

- F011 通过。

### Phase 4：端到端语音闭环

目标：

- 唤醒。
- 录音。
- STT。
- Claude CLI。
- 摘要。
- TTS。

完成标志：

- F012 通过。

## 11. 用户决策与后续增强项

已确认：

- 第一版平台：macOS。
- 第一版语言：Python。
- 第一版 TTS：macOS `say`。
- 第一版 STT：先做接口和 mock，真实 STT 可后续接入。
- 第一版唤醒：先做接口和按键/命令行触发，真实唤醒词后续接入。
- 最终要做 macOS 常驻 App。
- 不需要开机自动启动。
- 允许 App 运行期间常驻监听麦克风。
- Claude CLI 权限：不默认跳过权限，不默认执行 push/deploy/delete。
- 提交策略：每个 feature 一个 commit。

仍可后续选择的增强项：

- 真实 STT 使用云端 Whisper API、本地 whisper.cpp，还是 Apple Speech。
- 真实唤醒词使用 Porcupine、openWakeWord，还是继续只用按键唤醒。
- macOS 常驻 App 的形态：菜单栏 App、普通窗口 App，或二者都支持。
- 是否需要后台 launch agent。当前默认不需要开机自启，只需用户手动打开 App 后常驻运行。
- 是否需要语音确认高风险动作。

## 12. 禁止事项

- 不要一上来做完整桌面 App。
- 不要在没有测试的情况下把 feature 标为通过。
- 不要删除或弱化 `feature_list.json`。
- 不要把 Claude CLI 的长输出只打印不记录。
- 不要默认执行外部可见或破坏性动作。
- 不要让一次 coding agent 同时做多个 feature。
- 不要依赖上一轮对话上下文；必须依赖文件和 git history 接续。

## 13. 最终目标

最终可用形态：

```text
用户说出唤醒词
  -> Agent 开始录音
  -> 将语音转文字
  -> 判断风险等级
  -> 必要时要求确认
  -> 调用 Claude CLI 执行任务
  -> 记录 stdout/stderr/exit code
  -> 生成短摘要
  -> 用语音播报结果
```

第一版验收重点不是电影感，而是可靠、可测、可接续、安全。
