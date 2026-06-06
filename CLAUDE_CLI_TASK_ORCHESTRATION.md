# Claude CLI 任务编排说明

本文档说明本项目开发时如何由 Codex 编排 Claude CLI 完成 GitHub 上拆分好的任务。目标是控制单次上下文长度、降低套餐消耗、减少跨阶段上下文污染，并让每个任务都有明确的验收、提交和状态更新。

## 角色分工

### Codex：任务调度与验收者

Codex 负责：

- 读取 GitHub 仓库、Issues、Projects、PR、任务分解文档等任务来源。
- 选择当前要执行的最小任务单元。
- 为 Claude CLI 编写边界清晰的任务 prompt。
- 启动一个全新的 Claude CLI 会话执行当前任务。
- 在 Claude CLI 结束后检查代码 diff、测试结果、构建结果和任务完成度。
- 必要时要求 Claude CLI 返工，或由 Codex 直接修复小问题。
- 验收通过后提交代码、推送分支、更新 GitHub 任务状态。
- 关闭当前 Claude CLI 会话，再为下一个任务启动新的 Claude CLI。

### Claude CLI：短上下文执行者

Claude CLI 负责：

- 只处理 Codex 指派的单个任务。
- 根据任务说明阅读必要代码和任务上下文。
- 实现代码、补充测试、更新必要文档。
- 在结束前说明修改了哪些文件、如何验证、还有哪些风险。
- 不主动扩大任务范围，不处理未被指派的后续阶段。

### GitHub：任务源与交付记录

GitHub 负责承载：

- Issues / Projects / Milestones / PR checklist / Markdown 任务分解。
- 分支、提交、PR、CI 状态。
- 每个任务的完成状态和验收记录。

## 基本工作流

每个任务按以下流程执行：

1. Codex 同步仓库状态：

   ```bash
   git status
   git fetch --all --prune
   ```

2. Codex 读取 GitHub 任务：

   - 优先读取用户指定的 Issue、Project item、PR checklist 或任务文档。
   - 如果用户没有指定任务，Codex 先列出候选任务，并选择最小、最明确、依赖最少的任务。

3. Codex 创建或切换任务分支：

   ```bash
   git switch -c task/<task-id>-<short-name>
   ```

   如果已有合适分支，则复用该分支。

4. Codex 启动 Claude CLI 执行任务：

   ```bash
   claude -p "<task prompt>"
   ```

   默认每个任务使用新的 Claude CLI 调用，不复用上一个任务的会话上下文。

5. Claude CLI 完成后，Codex 检查结果：

   ```bash
   git diff
   git status
   ```

   并根据项目类型运行对应验证命令，例如：

   ```bash
   npm test
   npm run lint
   npm run build
   ```

   或项目中已有的测试、构建、格式化命令。

6. Codex 判断是否验收通过：

   - 通过：提交并推送。
   - 未通过：生成返工 prompt，启动新的 Claude CLI 修复，或由 Codex 直接修复小问题。

7. Codex 提交代码：

   ```bash
   git add <changed-files>
   git commit -m "<type>: <task summary>"
   git push -u origin <branch>
   ```

8. Codex 更新 GitHub 状态：

   - 在 Issue / Project / PR 中记录完成情况。
   - 关联 commit 或 PR。
   - 标记任务状态为 Done / In Review / Ready for Review，具体取决于项目约定。

9. Codex 关闭当前任务上下文，进入下一任务。

## Claude CLI 任务 Prompt 模板

Codex 指挥 Claude CLI 时应使用类似模板：

```text
你是本仓库的短上下文执行代理。只完成下面这个任务，不要扩大范围。

任务来源：
- GitHub Issue / Project / 文档：<link-or-id>
- 任务标题：<title>

任务目标：
<明确说明要实现什么>

验收标准：
- <criterion 1>
- <criterion 2>
- <criterion 3>

范围限制：
- 只修改与本任务直接相关的文件。
- 不处理其他未指派任务。
- 不做大规模重构，除非完成本任务必须如此。
- 不提交代码，不 push，不更新 GitHub 状态；这些由 Codex 完成。

执行要求：
- 先阅读相关代码和任务说明。
- 实现必要代码和测试。
- 运行项目中合适的验证命令。
- 结束时输出：
  1. 修改摘要
  2. 修改文件列表
  3. 已运行的验证命令及结果
  4. 未解决风险或后续建议
```

## 验收规则

Codex 验收时至少检查：

- `git diff` 是否只包含当前任务相关变更。
- 是否满足 GitHub 任务的验收标准。
- 是否引入明显回归、硬编码、临时代码或无关重构。
- 测试、lint、build 是否通过；如果无法运行，要记录原因。
- 文档、配置、迁移、环境变量是否同步更新。
- 是否存在安全、数据兼容、部署或回滚风险。

如果验收失败，Codex 应优先给 Claude CLI 一个具体返工任务，而不是让它重新理解整个项目。

## 上下文控制策略

- 一个 Claude CLI 会话只处理一个任务。
- 每个任务 prompt 只包含必要上下文，不粘贴整个项目说明。
- 大任务必须拆成多个可验收的小任务。
- 跨任务信息沉淀到代码、测试、文档、Issue 评论或 PR 描述中，而不是依赖 Claude CLI 的历史上下文。
- 下一阶段任务使用新的 Claude CLI 会话重新启动。

## GitHub 任务选择策略

优先选择满足以下条件的任务：

- 验收标准明确。
- 依赖少。
- 修改范围小。
- 可以独立测试。
- 完成后能产生可提交的代码增量。

暂缓以下任务：

- 目标模糊。
- 需要产品决策。
- 依赖未完成的上游任务。
- 涉及高风险数据迁移或生产配置，且没有明确回滚策略。

## 提交与 PR 约定

推荐提交信息：

```text
feat: implement <task summary>
fix: correct <bug summary>
test: add coverage for <behavior>
docs: document <topic>
chore: update <tooling/config>
```

推荐 PR 描述包含：

```markdown
## Summary
- ...

## Task
- Closes #<issue-number>

## Verification
- [ ] npm test
- [ ] npm run lint
- [ ] npm run build

## Notes
- ...
```

## 权限与安全边界

Claude CLI 默认不负责：

- 提交代码。
- push 到远端。
- 合并 PR。
- 删除分支。
- 修改生产密钥、真实用户数据、计费配置或部署配置。
- 执行不可逆操作。

这些动作由 Codex 在验收后执行，或在需要时向用户确认。

## 常用命令

检查 Claude CLI：

```bash
command -v claude
claude --version
claude --help
```

非交互执行：

```bash
claude -p "完成当前任务，并在最后输出修改摘要、验证命令和风险。"
```

查看 Claude CLI 的 MCP 和插件状态：

```bash
claude mcp list
claude plugin list
```

当前约定下，Claude CLI 作为本地命令执行；如果未来为 Claude CLI 配置 MCP 或插件，应在本文件中记录配置方式、用途和限制。

## 本项目执行原则

本项目开发采用：

```text
GitHub 任务源
  -> Codex 读取并拆分任务
  -> Codex 启动全新 Claude CLI 执行单个任务
  -> Codex 验收 diff / test / build / acceptance criteria
  -> Codex 提交、推送、更新 GitHub 状态
  -> 下一任务重新启动新的 Claude CLI
```

核心原则：Claude CLI 负责短上下文执行，Codex 负责长期编排与质量控制。
