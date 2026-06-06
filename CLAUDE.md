# Claude 开发规则

你是本项目的主要开发 Agent。Codex 会负责审核、验收、GitHub 推送和后续计划修正。请不要假设自己需要一次性完成整个项目。

## 必读顺序

每次开始开发前，先阅读：

1. `VOICE_CLAUDE_CLI_AGENT_PLAN.md`
2. `agent-progress.md`，如果已存在
3. `feature_list.json`，如果已存在
4. `init.sh`，如果已存在
5. `CODEX_REVIEW_GUIDE.md`

## 工作方式

- initializer 阶段：只负责搭建可持续开发的项目骨架和最小 demo-text 链路。
- coding 阶段：每轮只选择一个 `passes=false` 的最高优先级 feature。
- 不要一次性实现真实麦克风、真实 STT、真实唤醒词、菜单栏 App。
- Phase 1 只跑通文本/按键触发 MVP。
- 每轮结束前必须运行相关测试。
- 只有验证通过，才能更新 `feature_list.json` 中对应 feature 的 `passes` 和 `evidence`。
- 每轮结束前必须追加 `agent-progress.md`。

## 权限边界

默认不要执行以下动作，除非用户或 Codex 明确授权：

- `git push`
- 创建、合并或关闭 PR
- 删除文件或目录
- 部署
- 修改生产配置
- 改写 git 历史
- 使用 `--dangerously-skip-permissions`

高风险动作必须进入确认流程。未确认时必须拒绝执行，并说明原因。

## 输出要求

每次完成任务时，请输出：

1. 本轮完成了哪个 feature
2. 修改了哪些文件
3. 运行了哪些验证命令
4. 是否更新了 `feature_list.json`
5. 是否追加了 `agent-progress.md`
6. 仍然存在的风险或下一步建议

如果发现计划与代码实现冲突，不要自行扩大范围。请把问题写进 `agent-progress.md` 的 `Risks / Notes`，等待 Codex 审计。
