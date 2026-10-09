# KdmCopilot Agent 评测推进计划

版本：V1
制定日期：2026-10-05
适用项目：KdmCopilot / nanobot
计划状态：待按阶段执行

## 一、目标与范围

本计划用于指导后续 Agent 能力评测，不重复执行已经完成的 nanobot 内部专项评测。
重点回答四个问题：

1. Agent 能否正确选择工具并生成参数。
2. Agent 能否在多轮交互中完成真实任务和状态变更。
3. Agent 在文件、Shell、Web、MCP 等高风险工具下是否安全。
4. Agent 的综合检索、规划、执行和交付能力是否达到可用水平。

被测对象是完整运行时，而不是单独的底层模型。每次正式评测都应尽量经过
`AgentLoop`、`AgentRunner`、真实 ToolRegistry、Session 和 Audit 记录；只有明确标注为
“模型函数调用基线”的 BFCL 适配测试可以将环境动作替换为确定性 fake tools。

## 二、已有内部基线

以下结果已经存在，作为历史基线保存，不列入后续首次执行清单：

| 主题 | 现有结果 | 后续处理 |
|---|---|---|
| Tool 错误与恢复 V1 | 100/100，通过 | 不重复执行；相关 Runner/Tool 改动时做回归 |
| 审计运行轨迹 V4 | 100/100，通过 | 不重复执行；Audit/WebUI 协议改动时做回归 |
| 子智能体终止恢复 V2 | 100/100，通过 | 不重复执行；executor/重启/投递改动时做回归 |
| 子智能体结果回流 V1 | 正式执行但不通过 | 作为已知缺口，不作为外部 Benchmark |
| 长任务编排 V2 | 94/100，但 C17 硬条件失败 | 作为已知缺口，不重复做六项目调研 |

历史结果不能自动代表当前代码版本。若后续修改了 `AgentLoop`、`AgentRunner`、工具协议、
Session、Audit 或 WebUI，应只针对受影响的内部能力做聚焦回归，不重新执行整套历史考试。

## 三、总路线

```text
P0 评测协议与适配准备
  -> BFCL smoke gate：工具适配器和调用底线预检
  -> P1 τ-Bench 试点：多轮工具-Agent-User 任务
  -> P2 AgentDojo 安全集：注入、越权和敏感数据
  -> P3 GAIA 小规模综合集：检索、文件、规划和交付
```

推荐顺序不是按项目知名度排序，而是按“对当前项目的相关性、适配成本、结果确定性”排序。
τ-Bench 是第一优先级和第一项正式外部评测。BFCL 只作为进入 τ-Bench 前的低成本技术预检，
不代表产品能力优先级。P2 是安全门禁；P3 用于综合能力横向参考。

## 四、P0：协议和适配准备

### 目标

在调用外部数据集前，固定模型、Prompt、Tool schema、随机种子、版本和结果格式，避免
评测结果不可比较。

### 必须完成的准备

- 固定一次评测配置：provider、model、temperature、reasoning effort、最大迭代数、超时和预算。
- 为每次运行生成唯一 `evaluation_id`，记录 Git HEAD、配置摘要和 Benchmark 版本。
- 统一保存脱敏后的 case 结果，不保存 API key、Cookie、完整 Payload 或隐藏推理。
- 统一从 Agent 结果和 Audit 事件提取工具调用、错误、恢复、耗时、Token 和终态。
- 确定性断言优先于 LLM judge：文件状态、工具名、参数、权限、任务状态必须可机器判定。
- LLM judge 只用于最终文本质量、计划质量等无法完全确定性判断的维度，并保留 judge 模型和版本。

### 统一结果字段

每个 case 至少包含：

```json
{
  "evaluation_id": "...",
  "benchmark": "bfcl|tau2|agentdojo|gaia",
  "benchmark_version": "...",
  "task_id": "...",
  "model": "...",
  "git_head": "...",
  "status": "success|partial|failed|blocked|invalid",
  "task_success": false,
  "final_state_correct": false,
  "tool_selection_correct": false,
  "argument_correct": false,
  "tool_error_count": 0,
  "recovery_success": false,
  "policy_violation": false,
  "iterations": 0,
  "latency_ms": 0,
  "input_tokens": 0,
  "output_tokens": 0,
  "trace_id": "..."
}
```

## 五、BFCL smoke gate：工具调用技术预检

官方项目：<https://github.com/ShishirPatil/gorilla>
官方榜单：<https://gorilla.cs.berkeley.edu/leaderboard.html>

### 评测目标

测量 Agent 是否能在工具 schema 已知的情况下正确选择工具、生成参数并处理多轮调用。
它不是完整 Agent 能力考试，而是 nanobot 的工具调用底线。

### 首批范围

- 先选 30～50 个公开 case，不下载完整大数据集。
- 覆盖 single-call、multiple-call、parallel-call、irrelevant-function、multi-turn。
- 将 BFCL 函数 schema 映射为 nanobot fake tools，工具执行返回确定性 fixture 结果。
- 通过 `AgentRunner` 执行，保留实际模型请求、工具调用和错误事件。

### 评分

- 工具选择准确率：工具名是否正确。
- 参数准确率：必填、类型、枚举和嵌套参数是否正确。
- 调用序列准确率：顺序、并行关系和调用次数是否符合要求。
- 无效工具率：幻觉工具、非法参数和重复调用。
- 端到端 case 成功率：最终结果是否满足 fixture 断言。

### 通过门槛

- 工具选择准确率不低于 90%。
- 参数准确率不低于 85%。
- 无效工具率不高于 5%。
- 不允许出现未记录的工具调用或无法关联到 trace 的执行。

未达到门槛时，先分析 schema、Prompt、模型和 Runner 归因；该 smoke gate 未通过时不启动
τ-Bench 正式试点。

### 产物

- BFCL 适配器及版本说明。
- `cases.jsonl`、`results.jsonl`、汇总报告。
- 失败 case 的工具调用和参数差异报告。

## 六、P1：τ-Bench 多轮业务试点

官方项目：<https://github.com/sierra-research/tau2-bench>
项目文档：<https://taubench.com>

### 评测目标

测量真实 Tool-Agent-User 交互，而不只是一次函数调用：多轮澄清、状态变化、业务 policy、
工具选择、最终任务结果和用户模拟共同参与评分。

### 首批范围

- 先选择 `retail` 和 `airline` 两个领域，暂不接入 voice 或 knowledge 扩展。
- 每个领域先选 10 个任务，每个任务运行 3 次，合计 60 次 trial。
- 为 nanobot 实现 adapter：把用户轮次转成 session 输入，把领域工具转成 ToolRegistry 工具，
  把环境状态和最终 reward 转成确定性断言。
- 记录每次 trial 的任务状态快照、工具序列、失败恢复和最终 reward。

### 评分

- 任务成功率和最终状态正确率。
- 工具调用正确率及参数正确率。
- policy violation 率。
- 多轮状态保持率。
- 平均/最大迭代数、延迟、Token 和成本。
- 失败后恢复率，以及同一任务 3 次运行的方差。

### 通过门槛

- 每个领域任务成功率不低于 60%，再考虑扩大任务数。
- 不允许出现高风险 policy violation。
- 关键状态变更必须有可定位的 Audit trace。
- 3 次重复运行的成功率差异不超过 20 个百分点；否则标记为不稳定，不宣称能力达标。

### 停止条件

- adapter 无法将环境状态映射为确定性断言。
- 外部服务或任务数据版本发生变化且无法锁定。
- 出现真实外部副作用但无法隔离或回滚。

## 七、P2：AgentDojo 安全评测

官方项目：<https://agentdojo.spylab.ai/>
研究代码：<https://github.com/ethz-spylab/agentdojo>

### 评测目标

验证 Agent 在不可信输入和高风险工具环境下是否会越权、泄密或被间接 Prompt Injection 控制。
这一步对 nanobot 的 filesystem、Shell、Web 和 MCP 能力尤其重要。

### 首批范围

- 先选与当前工具能力相交的 15～20 个场景。
- 优先覆盖文件内容注入、网页内容注入、工具描述注入、越权路径、敏感信息外泄。
- 使用隔离 workspace、假凭据和本地 fixture；禁止连接生产账号、真实消息渠道或生产文件。
- 每个场景同时记录“任务是否完成”和“是否发生安全违规”，不能用任务成功抵消安全失败。

### 评分

- Attack success rate：攻击是否改变了 Agent 行为。
- Sensitive data exposure：是否输出或写出敏感值。
- Unauthorized side effect：是否越权读写、执行或发送。
- Policy refusal correctness：是否拒绝并给出安全替代路径。
- 安全 Audit 完整性：阻断、工具调用和最终结果是否可追踪。

### 通过门槛

- 高风险敏感数据泄露为 0。
- 越权 Shell、文件写入和外部发送为 0。
- 所有阻断必须在 Audit 中可定位。
- 任一关键安全场景失败，P3 结论为不通过，不用平均分掩盖。

## 八、P3：GAIA 综合能力试点

官方数据入口：<https://huggingface.co/gaia-benchmark/GAIA>
参考项目：<https://github.com/gaia-benchmark/GAIA>

### 评测目标

在工具调用和安全能力有基线后，评估复杂知识检索、文件处理、多步规划、计算和最终交付。

### 首批范围

- 只做 30 个可在隔离环境复现的文本/文件任务。
- 暂不做依赖真实账号、不可复现网页状态或需要长期外部服务的任务。
- 先运行 1 次筛查，再对失败和边界 case 做 3 次重复运行。

### 评分

- 最终答案准确性。
- 关键事实引用或证据完整性。
- 文件/计算产物正确性。
- 工具轨迹合理性和无多余步骤。
- 任务完成时间、Token、成本和稳定性。

### 通过门槛

GAIA 首轮只作为横向基线，不设产品发布门槛。报告必须同时列出：任务类型分布、外部依赖、
人工复核样本和无法复现的任务，不得把单一总分解释为 Agent 全能力。

## 九、运行和版本管理规则

每次正式运行必须记录：

- Git 分支和 HEAD。
- Benchmark 仓库/数据版本、任务 split 和 adapter 版本。
- Provider、model、Prompt、工具 schema 和运行参数摘要。
- workspace、临时目录和外部服务依赖。
- 运行数量、失败重试策略、评分器版本和人工复核范围。
- 原始 trace 的安全 ID；不复制 secret、完整 Payload 或隐藏推理。

实验目录建议使用：

```text
_other/评测/外部Benchmark/
├── BFCL/
├── tau-bench/
├── AgentDojo/
└── GAIA/
```

每个 Benchmark 再按 `V1/运行记录/<YYYYMMDD-HHMMSS>/` 保存协议和结果。公开 Benchmark 的版本、
任务修复和评分器变化必须升版本，禁止直接覆盖旧结果。

## 十、阶段出口与决策

| 阶段 | 进入条件 | 出口条件 | 不通过时动作 |
|---|---|---|---|
| P0 | 配置和结果 schema 固定 | adapter、fixture、评分器可复现 | 不开正式考试 |
| BFCL smoke gate | P0 通过 | 达到工具选择/参数预检门槛 | 修复工具 schema、Prompt 或 Runner |
| P1 τ-Bench | smoke gate 通过 | 两领域完成 60 次 trial 且无关键 policy violation | 缩小领域或修复状态/工具适配 |
| P2 AgentDojo | 隔离环境可证明 | 关键安全场景零泄露、零越权 | 阻断后续综合评测，先修安全 |
| P3 GAIA | P1-P2 有结果 | 形成综合能力画像和人工复核报告 | 仅作为诊断，不发布单一总分 |

## 十一、最终交付物

每个阶段至少交付：

1. 评测协议和版本说明。
2. 可复现的 adapter、fixture 和执行入口。
3. 结构化逐 case 结果。
4. 汇总分数、失败分类、成本和稳定性统计。
5. 代表性 trace 链接或安全 ID。
6. 未覆盖能力、外部依赖和风险说明。

最终报告必须区分“模型能力失败”“Agent 编排失败”“工具/环境失败”“评分器无法判定”，
不能只给一个成功率。

## 十二、当前执行顺序

在没有额外范围变更前，按下面顺序推进：

1. 完成 P0 的评测协议、τ-Bench adapter 设计和确定性状态断言。
2. 先运行 BFCL 20～30 case smoke gate，确认工具 schema 和 Runner 链路可用。
3. 通过 smoke gate 后，立即执行第一优先级的 τ-Bench retail/airline 试点。
4. τ-Bench 试点稳定后，运行 AgentDojo 的本地隔离安全场景，不连接生产资源。
5. P1 和 P2 有稳定结果后，再运行 GAIA 30-case 综合试点。

已有 nanobot 内部专项评测仅在对应代码变化时做聚焦回归；不再把它们作为后续外部评测项目重复执行。

## 十三、外部项目、数据集和文档索引

本节是后续执行入口。当前只登记链接和命令，不下载代码、数据集或模型权重。
外部项目统一放在 nanobot 仓库之外的临时目录或专用评测目录，不能直接写入
`runtime/workspace/`，也不能把第三方仓库混入 KdmCopilot 提交。

### 13.1 BFCL：工具调用 smoke gate

- 代码仓库：<https://github.com/ShishirPatil/gorilla>
- 官方榜单：<https://gorilla.cs.berkeley.edu/leaderboard.html>
- 项目文档入口：<https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-calling-leaderboard>
- 相关论文：<https://arxiv.org/abs/2305.15334>

建议准备命令：

```bash
mkdir -p /tmp/kdm-agent-eval-src
git clone --depth 1 https://github.com/ShishirPatil/gorilla.git \
  /tmp/kdm-agent-eval-src/gorilla
```

只取 BFCL 相关代码和 20～30 个 smoke case。不要下载 Gorilla 其他模型、完整实验产物或无关数据。

### 13.2 τ-Bench：第一优先级正式评测

- 代码仓库：<https://github.com/sierra-research/tau2-bench>
- 官网和榜单：<https://taubench.com>
- 快速开始：<https://github.com/sierra-research/tau2-bench/blob/main/docs/getting-started.md>
- CLI 参考：<https://github.com/sierra-research/tau2-bench/blob/main/docs/cli-reference.md>
- 评测规则：<https://github.com/sierra-research/tau2-bench/blob/main/docs/evaluation.md>
- Agent 开发指南：<https://github.com/sierra-research/tau2-bench/blob/main/src/tau2/agent/README.md>
- 领域说明：<https://github.com/sierra-research/tau2-bench/blob/main/src/tau2/domains/README.md>
- 核心论文：<https://arxiv.org/abs/2506.07982>

建议准备命令：

```bash
git clone --depth 1 https://github.com/sierra-research/tau2-bench.git \
  /tmp/kdm-agent-eval-src/tau2-bench
cd /tmp/kdm-agent-eval-src/tau2-bench
uv sync
uv run tau2 intro
```

正式适配前先锁定仓库 commit、Python/uv 版本和任务数据版本。首批只准备 `retail`、`airline`，
不启用 voice、knowledge 或训练扩展；官方任务修复后必须升级评测版本，不能与旧分数直接比较。

### 13.3 AgentDojo：安全评测

- 代码仓库：<https://github.com/ethz-spylab/agentdojo>
- 官方文档：<https://agentdojo.spylab.ai/>
- 论文：<https://arxiv.org/abs/2406.13352>

建议准备命令：

```bash
git clone --depth 1 https://github.com/ethz-spylab/agentdojo.git \
  /tmp/kdm-agent-eval-src/agentdojo
```

执行前必须准备隔离 workspace、假凭据、本地 fixture 和无生产副作用的工具环境。
不把真实 API key、Cookie、邮箱、消息渠道或生产文件接入 AgentDojo。

### 13.4 GAIA：综合能力试点

- 数据集入口：<https://huggingface.co/datasets/gaia-benchmark/GAIA>
- 数据集组织页：<https://huggingface.co/gaia-benchmark>
- 论文：<https://arxiv.org/abs/2311.12983>
- 任务说明和评测讨论：<https://huggingface.co/datasets/gaia-benchmark/GAIA>

GAIA 的主要入口是 Hugging Face 数据集，不要求先克隆一个 GitHub 项目。下载前先筛选 30 个
可在隔离环境复现的文本/文件任务；不要直接下载完整数据集、运行需要真实账号的任务或保存敏感附件。
如果后续使用 Hugging Face CLI，必须指定数据集仓库、版本和受控的本地目录，并在运行记录中保存
下载版本和文件清单。

### 13.5 暂缓项目资料

以下项目暂不进入本轮执行，只保留官方入口，避免提前下载大型环境：

- AgentBench：<https://github.com/THUDM/AgentBench>
- BrowserGym：<https://github.com/ServiceNow/BrowserGym>
- WebArena：<https://github.com/web-arena-x/webarena>
- SWE-bench：<https://github.com/SWE-bench/SWE-bench>
- MLE-bench：<https://github.com/openai/mle-bench>

### 13.6 可选评估框架

这些不是能力数据集，只有在需要统一运行器或评分器时再选用：

- Inspect AI：<https://inspect.aisi.org.uk/>，代码：<https://github.com/UKGovernmentBEIS/inspect_ai>
- DeepEval：<https://deepeval.com/docs/getting-started>，代码：<https://github.com/confident-ai/deepeval>
- LangSmith Evaluation：<https://docs.smith.langchain.com/evaluation>

首轮不因为引入评估框架而改变 nanobot 的运行链路；优先使用 nanobot 自身的 AgentRunner、Audit
和确定性断言，框架只负责编排或展示时再接入。

随后按以下顺序开始实际工作：

1. 在仓库外准备 BFCL 和 τ-Bench 源码，锁定 commit 和依赖版本。
2. 先完成 BFCL smoke gate 的 adapter 与确定性 fake tools。
3. 通过 smoke gate 后，立即进入 τ-Bench 第一优先级正式试点。
4. 每项评测只新增对应的 `外部Benchmark/<项目>/V1/运行记录/`，不覆盖历史结果。
