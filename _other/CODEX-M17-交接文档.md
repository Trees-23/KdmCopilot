# Codex 线程与 M17 工作交接文档

更新时间：2026-10-06（Asia/Shanghai）

## 1. 交接目标

下一位 Codex 请把本文档与最新可见对话一起阅读，然后从 M17 的真实 QQ 场景验收继续推进。不要因为 Codex 页面只显示到较早的“影子执行”节点，就重新设计 M17 或重复已经完成的代码工作。

## 2. 项目与 Git 状态

- 仓库：`/home/kdm/TL-WorkSpace/TL-Project/AIworker/nanobot-kdm-2k`
- 当前任务分支：`codex/memory-p1-trace`
- 目标基线：`origin/main`
- 当前 PR：`Trees-23/KdmCopilot#20`
- PR 标题：`功能（Skill进化）：完成 M17 恢复聚合与人工评论门禁`
- PR 状态：Open，非草稿，等待后续验收；未经用户明确确认，不要合并 `main`。
- M17 相关最新提交：
  - `915e885d`：补充候选开关关闭时的验收预期
  - `710309c4`：正确回填旧纠正方向
  - `9505791f`：兼容旧记录的分类回填
  - `0e23a3f5`：增加结构化聚合与人工评论门禁
- 工作区存在用户已有的未提交改动：`_other/评测/Agent评测推进记录.md`。不要覆盖、还原、暂存或提交它。

## 3. M17 已经完成的内容

M17 的目标是把 M16 的失败恢复记录从“按文件名/完整句子分散记录”升级为结构化语义聚合，并要求人工评论后才能申请修复候选。

已实现：

1. 恢复 Episode 持久化四类稳定语义字段：
   - `operation_family`
   - `failure_family`
   - `task_family`
   - `correction_family`
   同时记录分类版本、受限错误信息、来源和可重试性。
2. 文件名、日期、编号变化不会拆散同类；`file_not_found`、权限错误、超时等异常不会互相误合并。
3. 默认 90 天滚动窗口内，同一长期 QQ 会话累计两次同类且实际成功的恢复，才生成一个脱敏 `pending_review` Issue。
4. 单次恢复、分类不一致、证据不足或 90 天外证据，只保留 Case/审计，不生成 Issue。
5. 管理员必须先执行：

   ```text
   /evolve recovery note <Issue编号> <修复方向和边界>
   ```

   才能执行：

   ```text
   /evolve recovery accept <Issue编号> request_candidate
   ```

6. 有评论也只进入隔离 candidate revision，不会直接修改当前 Skill、创建 Proposal、创建 Draft PR 或发布；后续仍必须经过 M15 A/B、安全、反例、性能和独立回放 Gate。
7. 新增迁移：`0011_recovery_semantic_families`。
8. 已完成聚焦测试、迁移测试、Issue 状态测试、隔离端到端测试和相关 `ruff` 检查。
9. 回滚开关：`recovery_review_enabled` 或 `recovery_skill_candidate_enabled`。关闭开关不会删除既有 Issue、评论和审计，也不会改变当前生效 Skill。

详细设计和场景位于：

`_other/QQ-GROUP-SKILL-EVOLUTION-IMPLEMENTATION-PLAN.md:805`

## 4. 当前真实进度

最新 Codex 线程中的 M17 完成汇报已经明确：

- M17 实现、测试和部署已完成。
- 全量测试：`5506 passed, 18 skipped, 1 deselected`。
- 聚焦测试：`214 passed`。
- `ruff` 和 `git diff --check` 已通过。
- PR #20 已存在并开放。
- Gateway 代码/迁移已部署过，但 M17 的真实 QQ 场景证据尚未补齐。

因此当前准确状态是：**代码完成，隔离验证完成，长期 QQ 验收未完成。不要把 M17 宣称为完全验收。**

## 5. 下一步唯一主线：真实 QQ 场景验收

必须在授权 QQ 测试群、同一个长期会话中执行，且使用当前长期 Gateway。不要使用生产 Skill、长期记忆或无关目录。

依次发送以下无害消息，文件名故意不同：

```text
@小肯肯 请严格读取不存在的 m17-alpha-check.txt，不要换文件、不要猜测内容。
@小肯肯 刚才路径写错了，请读取 SOUL.md，并告诉我文件是否存在。
@小肯肯 请严格读取不存在的 m17-beta-check.txt，不要换文件、不要猜测内容。
@小肯肯 刚才路径写错了，请读取 README.md，并告诉我文件是否存在。
```

预期：12:00 汇总后生成一条脱敏“待评论” Issue，而不是两条；不会生成 Proposal。

然后执行：

```text
@小肯肯 /evolve recovery list pending
@小肯肯 /evolve recovery review <Issue编号>
@小肯肯 /evolve recovery accept <Issue编号> request_candidate
```

第三条预期被拒绝，并明确要求先提交人工 `note`。

再执行：

```text
@小肯肯 /evolve recovery note <Issue编号> 以后遇到工作区文件不存在时，先核对相对路径；不要扩大读取范围，也不要猜测文件内容。
@小肯肯 /evolve recovery accept <Issue编号> request_candidate
```

当前运行态 `recovery_skill_candidate_enabled=false` 时，预期是明确提示候选开关关闭；即使未来单独打开，也只能进入隔离候选队列并等待 M15，不得直接创建 Proposal 或修改生效 Skill。

验收必须记录：构建标识、Gateway 容器、运行根目录、QQ 场景提示词、Issue 通知、`review` 内容、无评论拒绝、`note` 后分流结果、审计/Trace URL，以及未覆盖风险。

## 6. 不要重复做的事情

- 不要重做 M17 设计、迁移或测试。
- 不要把不同文件名当成不同故障类型重新实现。
- 不要因为一次恢复就创建 Skill、Proposal 或 PR。
- 不要直接打开 `adoption`、`publish` 或修改当前生效 Skill。
- 不要合并 PR #20 到 `main`，除非用户明确确认。
- 不要删除 `runtime/workspace/`、长期 Gateway 数据、QQ 会话或审计记录。
- 不要修改 `_other/评测/Agent评测推进记录.md` 的用户未提交内容。

## 7. Codex 原线程损坏情况

用户提供的线程：`codex://threads/01a0d939-55f3-7721-90ba-af259a3cd3c1`

原始 rollout 仍完整存在：

`/home/kdm/.codex/sessions/2026/09/25/rollout-2026-09-25T23-40-08-01a0d939-55f3-7721-90ba-af259a3cd3c1.jsonl`

该文件已经写到 ordinal `29357`，其中包含 2026-10-05 的 M17 完成消息。因此最新对话并未从磁盘消失。

损坏的是 Codex 的历史投影数据库：

`/home/kdm/.codex/thread_history_1.sqlite`

当前线程投影状态约为 `next_rollout_ordinal=7576`，而 VS Code/Codex 日志反复报：

```text
thread history projection ... expected ordinal 7576, got 7575
```

这就是页面重启后回到较早节点的原因。由于 VS Code 会自动重启 Codex app-server，当前没有继续做数据库修复；不要删除原始 rollout。若以后要修复显示，应先完全退出 VS Code，再备份 SQLite 及其 `-wal`/`-shm` 文件，然后从 rollout 重建该线程投影。

## 8. 接手后的第一句话

接手后先回复用户：

> 我已读取交接文档。M17 代码、测试和 PR 已完成，当前只差长期 QQ 三段场景验收；我会先核对 Gateway 构建和运行态，再执行验收，不会重做 M17 或直接改 Skill。
