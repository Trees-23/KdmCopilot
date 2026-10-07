This file provides guidance to AI coding agents working with this repository.

## 仓库 Git 与交付流程

本仓库继承用户的全局 Git 与 PR 偏好，并补充以下规则：

- 面向当前 fork 开发时，任务分支以 `origin/main` 为基线，PR 目标为
  `Trees-23/nanobot-kdm-2k:main`。
- `upstream` 指向项目所关联的外部上游仓库。除非用户明确说要“拉取上游更新”或“同步上游”，
  不得对 `upstream` 执行 `fetch`、`pull`，也不得把 `upstream` 的提交 merge 或 rebase 到本项目。
- 只有用户明确要求向上游贡献时，才以 `upstream/main` 为基线或向 `HKUDS/nanobot` 提交 PR。
- 不得从脏的本地 `main` 或包含无关本地提交的分支开始任务。默认在当前仓库目录使用
  `git switch` 创建或切换任务分支；除非用户明确要求，不得创建 worktree 或额外项目目录。
- 如果受保护的本地修改阻止安全切换，先向用户报告，不得自动 stash、reset、clean、覆盖修改或
  改用 worktree 绕过。
- Codex 创建的提交说明、PR 文本和合并里程碑说明必须遵循全局指令并全部使用中文。
- 每个经过验证的工作单元都要推送，并持续维护同一个 PR。未经用户明确确认，绝不合并到 `main`。
- 同一任务的后续提交都推送到同一个功能分支并自动进入同一个 PR，不需要用户逐个确认提交；任务全部
  完成后，只在合并整个 PR 到 `main` 前询问一次。
- 用户明确确认合并后，合并工作同时包含远端和本地两部分：先完成远端 PR 到 `origin/main` 的合并，
  再切换到本地 `main`，使用仅快进方式同步 `origin/main`，并核对本地与远端默认分支指向同一提交。
  不得只完成远端合并而遗漏本地更新。
- 如果本地修改、分支分叉、网络或权限问题阻止本地 `main` 安全同步，不得 stash、reset、强制更新或
  覆盖用户改动；应保留现场并明确报告远端合并结果、本地未同步原因和当前提交差异。
- 合并后的交付说明必须分别列出远端 `origin/main` 与本地 `main` 的提交编号和同步状态；只有两者均已
  更新并核对一致时，才可表述为合并流程完成。
- 仅文档改动需要审查聚焦的差异，并检查链接和命令是否合理。Python 改动需要运行最接近的
  `pytest` 测试，并对改动涉及的 Python 路径运行 `ruff check`。WebUI 改动需要运行最接近的
  `bun run test` 测试；影响构建行为或捆绑产物时还要运行 `bun run build`。混合改动需要验证
  两套技术栈。
- 修改 `nanobot/agent/loop.py` 或 `nanobot/agent/runner.py` 时，除非能够证明不影响行为，否则必须
  补充或运行聚焦的集成测试。修改安全边界时必须验证相应的拒绝或路径约束行为。
- 不要运行 `ruff format`；本仓库明确避免大范围格式化造成的历史噪声。机械清理不得混入功能提交。
- 如果改动影响核心 Agent 流程、提示词行为、持久化、安全边界、配置兼容性或 WebUI 协议契约，
  必须在 PR 正文中明确说明。

## 运行目录卫生

- `runtime/workspace/` 是用户的长期默认工作区。未经用户针对该目录的明确授权，绝不删除、移动、
  清空或覆盖该目录及其内容。
- 长期 Gateway 仅使用 Compose 服务 `nanobot-gateway`，固定占用 `8765`（WebUI）和 `18790`（健康检查），
  并挂载完整的 `runtime/` 运行根目录；其默认工作区必须保持为 `runtime/workspace/`。不得因切换 Git
  分支、端口占用或日常验收自动启动另一套 Gateway、改用其他端口或改用其他运行根目录。
- 默认场景验收在这套长期 Gateway 的全新 WebUI 会话中完成。只有用户明确要求并行或隔离实验时，才可创建
  临时 Gateway；临时运行根目录统一创建在 `runtime/.tmp/<日期>-<任务标识>/`，并使用该目录中的
  `workspace/`。
- 临时实例只允许用于当前任务。验证结果已记录、相关进程已停止且不需要保留复现环境时，删除由当前
  任务创建的整个 `runtime/.tmp/<...>/` 目录；若 `runtime/.tmp/` 为空，也一并移除该空父目录。
- 删除前必须逐项确认目标位于 `runtime/.tmp/`、目录确由当前任务创建、且没有仍在使用该目录的进程。
  不得使用宽泛通配符、不得删除 `runtime/` 根目录，也不得清理既有历史运行目录，除非用户明确指定。
- 若用户要求保留验收证据或复现环境，保留对应临时目录并在交付说明中标明路径和保留原因；不得自行
  推断可以删除用户创建或先前任务留下的目录。

## Docker 构建与真实场景验收

- 修改 Python、WebUI、Dockerfile、依赖或运行协议后，不得只重启既有 `nanobot-gateway` 容器。完成任务
  提交后必须执行 `./scripts/rebuild_gateway_for_scenario.sh`；脚本只重建并替换固定的长期 Gateway，要求
  干净工作区，并确认健康检查返回的构建标识与脚本输出一致后，才能宣称 Gateway 使用了本次代码。
- 需要让长期 Gateway 加载新提交或新配置时，必须分步操作，避免停止、构建、启动混在一次排障动作中：
  1. 先执行 `docker compose stop nanobot-gateway`；
  2. 再执行 `docker compose ps nanobot-gateway`、健康端口探测和固定端口占用检查，确认容器已停止、
     `18790` 不可连接且 `8765/18790` 没有残留的非 Compose 容器；
  3. 只有确认停止成功后，才能执行 `./scripts/rebuild_gateway_for_scenario.sh`，由脚本完成构建、重建和启动；
  4. 启动后必须核对健康检查中的构建标识、容器 ID、镜像 ID、Compose 服务名、`runtime/` 挂载和实际工作区。
  任一步确认失败都应停止后续操作并报告，不得用 `docker compose restart`、第二套 Gateway、临时端口或
  `docker compose down -v` 绕过该顺序。
- 默认地址固定为 `http://localhost:8765`、`http://localhost:8765/#/new`、
  `http://localhost:8765/#/traces` 和 `http://127.0.0.1:18790/health`。若这些端口被非 Compose 容器占用，
  明确报告冲突容器并退出；不得自动换端口或接管、停止、删除该容器。
- 涉及 Agent 流程、工具、审计、持久化、WebUI 协议或用户可见行为的改动，除聚焦测试外，默认必须完成
  一次真实 Gateway 场景验收；该验收已获授权，可使用当前配置的模型和全新的 WebUI 会话。
- 场景提示词必须针对本次改动、带唯一场景标识，并明确预期回答、工具行为或审计状态。优先核对实际
  Agent 回答、工具执行、运行轨迹图、事件时间线和前端展示，不得以代码测试替代场景验收。
- 场景需要写入时，只能使用当前任务约定的测试目标或工作区；不得改动长期记忆、配置、凭据、生产仓库
  或无关目录。若场景本身需要外部消息、远程资源或第三方数据变更，必须在执行前说明目标和预期副作用，
  并限制在指定测试对象。
- 每次场景验收交付必须报告：构建标识、WebUI 新会话 URL、运行轨迹 URL、具体 trace URL、场景提示词、
  预期结果、实际结果和未覆盖风险。不得在交付中输出 WebUI bootstrap secret、API Key 或其他凭据。

## 部署一致性与场景证据

- 开始真实场景前，必须记录并相互核对当前 Git 分支和 HEAD、Gateway 容器 ID、镜像 ID、构建标识、
  Compose 服务名、`runtime/` 挂载路径以及 Agent 实际工作区。任一项与当前代码或任务预期不一致时，
  先判定为部署错配，不得用旧实例的页面或轨迹作为验收结论。
- 涉及 HTTP API、WebSocket、审计或轨迹协议的改动，必须验证实际接口不是 404，鉴权后的响应状态和
  数据结构符合预期，并确认真实运行产生了对应事件。仅确认前端页面能打开或单元测试通过，不足以证明
  协议已接通。
- 场景提示词中的文件路径必须相对于 Agent 实际工作区编写。执行读、写、编辑或列表操作前，先确认
  目标文件确实存在于该工作区；不得把宿主机路径、其他运行目录或历史实例路径当作场景前提。
- 验证工具失败恢复时，必须保留真实失败、纠正动作和纠正后成功的关联证据，并在审计或轨迹中确认三者
  属于同一场景。普通的一次成功调用不能替代恢复能力验收。
- 验证多子 Agent 时，按提示词约定的数量逐项核对创建事件、执行终态、结果投递和父子关联；不能只
  根据最终汇总文本推断子 Agent 已实际运行。
- 使用 `nanobot-cli` 或其他独立 CLI 检查 schema、索引或审计数据前，先确认 CLI 与 Gateway 使用同一
  代码版本和构建标识，避免把版本不兼容误判为功能缺失。
- 构建缓存异常必须有具体证据（例如构建标识未变化或依赖层明显错配）后再使用 `--no-cache`；不得
  把无证据的全量重建作为默认排障步骤。任何情况下都不得使用 `docker compose down -v` 破坏持久化数据。

## Project Overview

nanobot is a lightweight, open-source AI agent framework written in Python with a React/TypeScript WebUI. It centers around a small agent loop that receives messages from chat channels, invokes an LLM provider, executes tools, and manages session memory.

## Development Commands

```bash
# Python: run single test / lint
pytest tests/test_openai_api.py::test_function -v
ruff check nanobot/

# WebUI: dev server (proxies API/WS to gateway :8765), build, test
# Build outputs to ../nanobot/web/dist (bundled into the Python wheel)
cd webui && bun run dev      # or NANOBOT_API_URL=... bun run dev
cd webui && bun run build
cd webui && bun run test

# Gateway
nanobot gateway
```

## High-Level Architecture

### Core Data Flow

Messages flow through an async `MessageBus` (`nanobot/bus/queue.py`) that decouples chat channels from the agent core:

1. **Channels** (`nanobot/channels/`) receive messages from external platforms and publish `InboundMessage` events to the bus.
2. **`AgentLoop`** (`nanobot/agent/loop.py`) consumes inbound messages, builds context, and coordinates the turn.
3. **`AgentRunner`** (`nanobot/agent/runner.py`) handles the actual LLM conversation loop: send messages to the provider, receive tool calls, execute tools, and stream responses.
4. Responses are published as `OutboundMessage` events back to the appropriate channel.

### Key Subsystems

- **Agent Loop** (`nanobot/agent/loop.py`, `runner.py`): The core processing engine. `AgentLoop` manages session keys, hooks, and context building. `AgentRunner` executes the multi-turn LLM conversation with tool execution.
- **LLM Providers** (`nanobot/providers/`): Provider implementations (Anthropic, OpenAI-compatible, OpenAI Responses API, Azure, Bedrock, GitHub Copilot, OpenAI Codex, etc.) built on a common base (`base.py`). Includes image generation (`image_generation.py`) and audio transcription (`transcription.py`). `factory.py` and `registry.py` handle instantiation and model discovery.
- **Channels** (`nanobot/channels/`): Platform integrations (Telegram, Discord, Slack, Feishu, Matrix, WhatsApp, QQ, WeChat, WeCom, DingTalk, Email, MoChat, MS Teams, WebSocket, Mattermost). `manager.py` discovers and coordinates them. Channels are self-contained packages auto-discovered via `pkgutil` scanning.
- **Tools** (`nanobot/agent/tools/`): Agent capabilities exposed to the LLM: filesystem (read/write/edit/list), shell execution (with sandbox backends), web search/fetch, MCP servers, cron, notebook editing, subagent spawning, long-running tasks / sustained goals (`long_task.py`), image generation, and self-modification. Tools are auto-discovered via `pkgutil` scan + entry-point plugins.
- **Memory** (`nanobot/agent/memory.py`): Session history persistence with Dream two-phase memory consolidation. Uses atomic writes with fsync for durability.
- **Session Management** (`nanobot/session/`): Per-session history, context compaction, TTL-based auto-compaction (`manager.py`), and sustained goal state tracking (`goal_state.py`).
- **Config** (`nanobot/config/schema.py`, `loader.py`): Pydantic-based configuration loaded from `~/.nanobot/config.json`. Supports camelCase aliases for JSON compatibility.
- **WebUI** (`webui/`): Vite-based React SPA that talks to the gateway over a WebSocket multiplex protocol. The dev server proxies `/api`, `/webui`, `/auth`, and WebSocket traffic to the gateway.
- **API Server** (`nanobot/api/server.py`): OpenAI-compatible HTTP API (`/v1/chat/completions`, `/v1/models`) for programmatic access.
- **Command Router** (`nanobot/command/`): Slash command routing and built-in command handlers.
- **Heartbeat** (`nanobot/templates/HEARTBEAT.md`): Periodic task list checked via `cron` jobs (legacy dedicated service removed).
- **Pairing** (`nanobot/pairing/`): DM sender approval store with persistent pairing codes per channel.
- **Skills** (`nanobot/skills/`): Built-in skill definitions (cron, github, image-generation, etc.) loaded into agent context.
- **Security** (`nanobot/security/`): PTH file guard and other security measures activated at CLI entry.

### Entry Points

- **CLI**: `nanobot/cli/commands.py`
- **Python SDK**: `nanobot/nanobot.py`

## Project-Specific Notes

- Architecture constraints: [`.agent/design.md`](.agent/design.md)
- Security boundaries: [`.agent/security.md`](.agent/security.md)
- Common gotchas: [`.agent/gotchas.md`](.agent/gotchas.md)

## Contribution Flow

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for contribution flow and PR guidelines.

## Code Style

- Python 3.11+, asyncio throughout.
- Line length: 100.
- Linting: `ruff` with rules E, F, I, N, W (E501 ignored).
- pytest with `asyncio_mode = "auto"`.

## Common File Locations

- Config schema: `nanobot/config/schema.py`
- Provider base / new provider template: `nanobot/providers/base.py`
- Channel base / new channel template: `nanobot/channels/base.py`
- Tool registry: `nanobot/agent/tools/registry.py`
- WebUI dev proxy config: `webui/vite.config.ts`
- Tests mirror the `nanobot/` package structure.
