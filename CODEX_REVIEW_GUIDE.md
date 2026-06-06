# Codex 审核与验收指南

Codex 是本项目的审核、验收、审计和 GitHub 发布 Agent。Claude Desktop / Claude CLI 是主要开发 Agent。

## Codex 职责

- 检查 Claude 的实现是否严格遵守 `VOICE_CLAUDE_CLI_AGENT_PLAN.md`。
- 检查每轮是否只完成一个 feature。
- 检查测试、自检、日志、风险控制和文档更新是否完整。
- 检查 `feature_list.json` 是否没有被删除、弱化或跳过验收。
- 检查 `agent-progress.md` 是否记录了本轮进展、验证和下一步任务。
- 审核通过后负责 git commit、push、创建 PR 或更新 GitHub。
- 如果实现偏离计划，把审计结论写入下一步工作计划或进度文件。

## 每轮验收清单

Codex 审核 Claude 输出时至少检查：

- `git status` 中的变更是否只属于当前任务。
- `git diff` 是否包含无关重构、临时代码或绕过安全限制。
- 是否运行了 `./init.sh check` 和 `./init.sh test`，如果尚未存在则检查原因是否合理。
- 是否有必要的单元测试。
- 高风险动作是否默认二次确认。
- Claude CLI 是否只通过受控 subprocess 执行。
- stdout、stderr、exit code 是否被记录。
- TTS、STT、wake trigger 是否保持可替换接口，而不是写死实现。

## 发现偏差时

如果 Claude 的实现偏离计划，Codex 不应直接把偏差合并到主线。优先处理方式：

1. 在审计结论中说明偏差。
2. 必要时直接修复小问题。
3. 对较大偏差，写入 `agent-progress.md` 的下一步计划。
4. 给 Claude 下一轮提供明确返工任务。

## GitHub 发布规则

- 主分支保持可读、可运行、可接续。
- commit 信息要小而清楚。
- 公开仓库中不得提交 API key、token、私密路径或真实用户数据。
- push 前必须检查 `git status`、`git diff --cached` 和最近 commit。
