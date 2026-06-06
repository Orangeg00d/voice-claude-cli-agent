# Voice Claude CLI Agent

一个面向 macOS 的本地语音 Agent 项目。目标是让用户通过按键或语音唤醒发出开发指令，由本地 Claude CLI 执行任务，并在完成后用语音播报结果。

## 当前目标

第一阶段先实现文本/按键触发 MVP：

```text
文本指令
  -> Claude CLI
  -> 结果记录
  -> 摘要
  -> macOS say 语音播报
```

真实麦克风、真实 STT、真实唤醒词和 macOS 常驻 App 会在后续阶段逐步加入。

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

## 已确认方向

- 平台：macOS。
- 第一版语言：Python。
- 第一版 TTS：macOS `say`。
- 第一版 STT：先做接口和 mock。
- 第一版唤醒：先做按键/命令行触发。
- 最终形态：macOS 常驻 App。
- App 不需要开机自动启动。
- 允许 App 运行期间常驻监听麦克风。
- 高风险动作默认必须二次确认。

## 开发状态

当前仓库仍处于计划与初始化阶段。下一步应由 Claude Desktop 执行 initializer 任务，建立 Python 项目骨架、`init.sh`、`feature_list.json`、`agent-progress.md` 和最小 demo-text 链路。
