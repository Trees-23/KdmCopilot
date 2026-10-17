# CLI Reference

Use this page when you know what you want to run and need the command shape. For a guided first run, start with [`quick-start.md`](./quick-start.md).

## Choose a Command

| Goal | Command | Notes |
|---|---|---|
| Check the install | `nanobot --version` | If this fails, try `python -m nanobot --version` |
| Create or refresh config | `nanobot onboard` | Creates `~/.nanobot/config.json` and `~/.nanobot/workspace/` |
| Refresh config non-interactively | `nanobot onboard --refresh` | Preserves existing values and adds missing default fields without prompting |
| Use guided setup | `nanobot onboard --wizard` | Best when you prefer prompts over hand-editing JSON |
| Open the browser workbench | `nanobot webui` | Prepares local WebUI settings, starts the gateway, and opens the browser |
| Check config without calling a model | `nanobot status` | Summarizes the selected config, workspace, active model, and providers |
| Send one test message | `nanobot agent -m "Hello!"` | First proof that install, config, provider, model, and workspace all work |
| Chat in the terminal | `nanobot agent` | Interactive local chat; exit with `exit`, `/exit`, `:q`, or `Ctrl+D` |
| Run the gateway directly | `nanobot gateway` | Service/ops command for WebUI, chat apps, cron, and heartbeat |
| Deliver a local trigger | `nanobot trigger <id> "message"` | Created first with `/trigger <name>` in the target chat/session |
| Serve an OpenAI-compatible API | `nanobot serve` | Starts `/v1/chat/completions`, `/v1/models`, and `/health` |
| Check chat channel setup | `nanobot channels status` | Useful before starting `nanobot gateway` |
| Manage optional features | `nanobot plugins list` | Shows channels and optional capabilities you can turn on |
| Log in to QR/OAuth-style channels | `nanobot channels login <channel>` | Used by channels such as WhatsApp and WeChat |
| Log in to OAuth model providers | `nanobot provider login <provider>` | Used by OAuth providers such as OpenAI Codex and GitHub Copilot |

## Global

```bash
nanobot --help
nanobot --version
python -m nanobot --help
python -m nanobot --version
```

`python -m nanobot ...` is useful when the package is installed but the `nanobot` script is not on `PATH`.

## 一次性 P6 历史数据清理

`nanobot memory purge-legacy` 仅用于自进化重构的 P6 清理。默认与 `--dry-run` 都是只读预览，
必须显式指定已有数据库路径；不创建数据库、不执行迁移、不清理文件目录：

```bash
nanobot memory purge-legacy --database runtime/workspace/.nanobot/memory.sqlite3 --dry-run
```

输出固定序 1→23 的表、筛选条件、操作、计数及 `plan_digest`。第 12 步包含两张评测预算派生表
`evaluation_candidate_admissions`、`evaluation_model_call_reservations`，先清子表再清评测。
序 19 先把待删 Skill 的 `current_revision_id` 置空，再删对应版本和 Skill；仅匹配
`skill:m19:%`、`skill:recovery-%`、`skill:evolution-%` 与 `skill-overlay-hot-reload-m9`。
全部 `retrieval_events` 及全部状态的 `memory_assets` 也在清理范围内。

真实执行必须先获清理授权，并停止使用该数据库的 Gateway、worker、独立 CLI、定时任务等所有
读写进程；停止期间重新预览、核对清单。`--writers-stopped` 是操作者确认，不能替代实际停进程。
工具额外检查数据库独占访问及有效维护/评测租约，无法取得独占访问或租约仍有效时拒绝执行。
并发清理执行者通过数据库旁的 `.p6.lock` 互斥文件串行进入 SQLite，再在写事务内核对完成标记；
该锁文件不是备份，也不是完成标记，不要在执行者运行期间删除。

确认后在同一停写窗口使用刚核对的摘要（下面的 `已核对的摘要` 需替换）：

```bash
nanobot memory purge-legacy --database runtime/workspace/.nanobot/memory.sqlite3 \
  --execute --writers-stopped --confirm-plan 已核对的摘要
```

本操作不备份、不删除表或索引。删除、提交前外键校验及固定完成标记
`data_purge_completions/self-evolution-p6-v1` 在一个独立事务内执行；失败全部回滚，外键恢复开启。
清单计数变化会拒绝旧摘要。首次成功后，重复执行只报告 `already_completed`，保留后来新产生的
证据、资产、检索事件和复用计数；不得删除或修改完成标记来重新执行。

审计、手工 Skill、L0 文件记忆、`maintenance_jobs` 和 `maintenance_lock` 不在本操作的删除范围。
五个历史运行目录需要另行授权和处理，数据库完成标记不代表文件已删除。
成功后应在恢复写入前核对清空结果，再按部署流程恢复 Gateway；此入口不会自动停启服务。

## 独立 C-P6 维护表清理

P6 完成后，`nanobot memory purge-maintenance --database <已有数据库> --dry-run` 只读预览
`maintenance_jobs` → `maintenance_lock` 两张表；默认行为同样是预览。它不属于 P6 的23步，
真实执行必须另获 C-P6 授权、暂停所有相关执行者、确认无有效租约，并重新核对预览摘要。

独立授权后的执行参数为 `--execute --writers-stopped --confirm-plan <最新摘要>`。
本入口保持外键开启，在独立事务内删除两表历史行、检查外键并写固定标记
`legacy-maintenance-cp6-v1`；表/索引与活锁函数保留。未完成P6时拒绝，失败回滚；
成功后重跑保留后来新写的维护任务和租约。锁文件与P6共享，完成标记分别记账。
不备份、不清 `memory_outbox`、资产、审计、P6标记或文件目录；不自动停启服务。

Q6 workspace 治理进入 `paused` 后，worker 会停止领取 review/lesson 任务，并保留队列等待独立审核。
审核后通过以下命令恢复；actor 和 reason 都是必填项：

```bash
nanobot memory governance-resume --actor reviewer-id --reason "复核了触发指标与 Skill 当前版本"
```

命令记录恢复代次和审计证据，保留首次容量基线。它不清零失败计数、不删除队列数据，也不会恢复旧周期过期清理器。
review 扫描按工作区持久状态先检查健康门禁，再进入候选评测；只有命中仍指向当前已采用版本的 workspace Skill 时才按基线版本 CAS 回滚。计数来自近一天失败门禁/权限步骤、独立审计索引中的 policy_blocked 元数据、投递死信与容量基线，不读取审计 payload。

## Common Patterns

Most day-to-day commands use the default config and workspace. Advanced or multi-instance runs usually pass both paths explicitly:

```bash
nanobot agent --config ./bot-a/config.json --workspace ./bot-a/workspace -m "Hello"
nanobot gateway --config ./bot-a/config.json --workspace ./bot-a/workspace
nanobot serve --config ./bot-a/config.json --workspace ./bot-a/workspace
```

Use `--verbose` on long-running processes when you need startup or runtime logs:

```bash
nanobot gateway --verbose
nanobot serve --verbose
```

Long-running commands keep working until you stop them. Press `Ctrl+C` in that terminal
to stop foreground `nanobot gateway` or `nanobot serve`. If you started the gateway
with `--background`, use `nanobot gateway stop`.

## Setup

| Command | Description |
|---|---|
| `nanobot onboard` | Initialize or refresh the default config and workspace |
| `nanobot onboard --refresh` | Refresh an existing config without prompting, preserving existing values |
| `nanobot onboard --wizard` | Use the interactive setup wizard |
| `nanobot onboard --config <path> --workspace <path>` | Initialize or refresh a specific instance |

Default paths:

| Path | Default |
|---|---|
| Config | `~/.nanobot/config.json` |
| Workspace | `~/.nanobot/workspace/` |

## Agent CLI

| Command | Description |
|---|---|
| `nanobot agent -m "Hello!"` | Send one message and exit |
| `nanobot agent` | Start interactive terminal chat |
| `nanobot agent --session <id>` | Use a specific session key |
| `nanobot agent --workspace <path>` | Override workspace |
| `nanobot agent --config <path>` | Use a specific config file |
| `nanobot agent --no-markdown` | Print plain text instead of Rich-rendered Markdown |
| `nanobot agent --logs` | Show runtime logs while chatting |

In interactive mode, `Enter` sends the current message. Press `Alt+Enter` to add a newline before sending.

Interactive mode exits with `exit`, `quit`, `/exit`, `/quit`, `:q`, or `Ctrl+D`.

## WebUI

| Command | Description |
|---|---|
| `nanobot webui` | Create config/workspace if needed, enable the local WebUI channel after confirmation, start the gateway, and open `http://127.0.0.1:8765` |
| `nanobot webui --background` | Start or reuse a background gateway, then open the WebUI |
| `nanobot webui --no-open` | Prepare and start the WebUI without opening a browser |
| `nanobot webui --port <port>` | Set the WebUI/WebSocket port |
| `nanobot webui --gateway-port <port>` | Override the gateway health port |
| `nanobot webui --yes` | Apply safe localhost WebUI defaults without confirmation; provider credentials still require interactive setup |

First-run WebUI setup binds to `127.0.0.1` by default. Use manual configuration and a WebUI password before exposing the WebSocket channel beyond localhost.

## Gateway

`nanobot gateway` starts enabled chat channels, WebUI/WebSocket when configured, cron-backed system jobs, Dream, heartbeat, and the health endpoint. Most local browser users should start with `nanobot webui`; use `gateway` directly for service management, chat app operation, and advanced deployment. By default it runs in the foreground, which keeps existing scripts and terminal workflows unchanged. Use `--background` when you want a local macOS, Linux, or Windows process that you can manage from the CLI.

| Command | Description |
|---|---|
| `nanobot gateway` | Start the gateway in the foreground with config defaults |
| `nanobot gateway --verbose` | Show verbose runtime output |
| `nanobot gateway --port <port>` | Override `gateway.port` for the health endpoint |
| `nanobot gateway --workspace <path>` | Override workspace |
| `nanobot gateway --config <path>` | Use a specific config file |
| `nanobot gateway --background` | Start the gateway as a background process |
| `nanobot gateway status` | Show the recorded background gateway PID, state file, and log file |
| `nanobot gateway logs --no-follow` | Print recent background gateway logs and exit |
| `nanobot gateway logs` | Follow background gateway logs |
| `nanobot gateway restart` | Restart the recorded background gateway with the current config |
| `nanobot gateway stop` | Stop the recorded background gateway |
| `nanobot gateway install-service` | Install a systemd user service or macOS LaunchAgent |
| `nanobot gateway install-service --dry-run` | Preview the generated service file and system commands |
| `nanobot gateway uninstall-service` | Remove the installed system service |

For custom instances, pass the same selector flags to management commands:

```bash
nanobot gateway --background --config ./bot-a/config.json --workspace ./bot-a/workspace
nanobot gateway status --config ./bot-a/config.json --workspace ./bot-a/workspace
nanobot gateway stop --config ./bot-a/config.json --workspace ./bot-a/workspace
nanobot gateway install-service --config ./bot-a/config.json --workspace ./bot-a/workspace --name bot-a
```

`--background` is a lightweight detached process. `install-service` is for
login/startup integration: Linux uses a systemd user service; macOS uses a
LaunchAgent plist. System services run the foreground gateway under the OS
supervisor rather than nesting another background process.

Default health endpoint:

```text
http://127.0.0.1:18790/health
```

The bundled WebUI is served by the WebSocket channel, usually on port `8765`, not by the gateway health endpoint.

## Local Triggers

`nanobot trigger` delivers one local message to a trigger that was created from
a chat/session with `/trigger <name>`.

```bash
nanobot trigger trg_8K4P2Q9X "Review PR #4502"
```

Keep `nanobot gateway` running so the message can be delivered to the linked
chat/session. The message is recorded as an automation turn in that session,
not as a normal chat message typed by the user.

The command writes to a workspace-local durable queue. If `nanobot gateway` is
not running yet, the message waits in that workspace. If the target session is
already running a turn, the trigger waits for that session to become idle. If the
gateway exits after claiming a delivery but before the linked turn completes,
the next gateway start requeues that delivery. The queue is at-least-once, not
exactly-once, so the same message can be delivered again after an interrupted
process. If the agent receives the delivery and the turn fails, the delivery is
marked failed instead of retried indefinitely. Each delivery also writes an
audit record under `<workspace>/triggers/runs`. Run one gateway consumer per
workspace; this local queue is not a distributed multi-consumer queue.

Use stdin when another local process generates the message:

```bash
generate-report | nanobot trigger trg_8K4P2Q9X
```

Options:

| Command | Description |
|---|---|
| `nanobot trigger <id> "message"` | Deliver one message through a trigger |
| `nanobot trigger <id>` | Read the message from stdin |
| `nanobot trigger --config <path> <id> "message"` | Use the workspace from a specific config |
| `nanobot trigger --workspace <path> <id> "message"` | Use a specific workspace |

Triggers are managed in the WebUI Automations view instead of through separate
`list`, `revoke`, or `delete` CLI subcommands. From there you can pause/resume,
rename, delete, search, and copy the command for each trigger.

For webhooks or other external systems, run your own small service and have it
call this CLI after it decides what message nanobot should receive.

See [Automations](./automations.md) for the broader automation model, WebUI
management, and delivery behavior.

## OpenAI-Compatible API

| Command | Description |
|---|---|
| `nanobot serve` | Start `/v1/chat/completions`, `/v1/models`, and `/health` |
| `nanobot serve --host <host>` | Override API bind host |
| `nanobot serve --port <port>` | Override API port |
| `nanobot serve --timeout <seconds>` | Override per-request timeout |
| `nanobot serve --verbose` | Show runtime logs |
| `nanobot serve --workspace <path>` | Override workspace |
| `nanobot serve --config <path>` | Use a specific config file |

Default API endpoint:

```text
http://127.0.0.1:8900
```

Public binds (`0.0.0.0` or `::`) require `api.apiKey`; send it as a Bearer token on API routes.

See [`openai-api.md`](./openai-api.md) for request examples.

## Status

```bash
nanobot status
```

Shows the config path, workspace path, active model, and provider summary without calling a model.

| Command | Description |
|---|---|
| `nanobot status` | Inspect the default instance |
| `nanobot status --config <path>` | Inspect a specific config |
| `nanobot status --config <path> --workspace <path>` | Inspect a specific config with a workspace override |

## Channels

| Command | Description |
|---|---|
| `nanobot channels status` | Show configured channel status |
| `nanobot channels status --config <path>` | Show channel status for a specific config |
| `nanobot channels login <channel>` | Run interactive login for supported channels |
| `nanobot channels login <channel> --force` | Re-authenticate even if credentials already exist |
| `nanobot channels login <channel> --config <path>` | Use a specific config file |
| `nanobot plugins list --config <path>` | Show plugin/channel enabled state for a specific config |

Examples:

```bash
nanobot channels login whatsapp
nanobot channels login weixin
nanobot channels status
```

See [`chat-apps.md`](./chat-apps.md) for channel-specific setup.

## Optional Features

Use these commands when you want nanobot to add or remove a built-in capability
without hand-editing JSON. Enabling may install the support package first.
Disabling is for channels such as Telegram, Matrix, or Slack; it keeps your
saved settings and turns the channel off.

The `plugins` command name is retained for compatibility, but these entries are
nanobot runtime support packages, not the user-invokable tools shown in WebUI
Apps. They cannot be attached to a chat turn with `@`.

| Feature name | What it enables |
|---|---|
| `api` | Dependencies required by the OpenAI-compatible `nanobot serve` process |
| `azure` | Azure identity support for Azure-hosted models |
| `bedrock` | AWS Bedrock model provider support |
| `langfuse` | Langfuse tracing support for OpenAI-compatible providers |
| `olostep` | Olostep web search provider support |
| A channel name such as `telegram` or `slack` | The connector package and saved channel enablement |

| Command | Description |
|---|---|
| `nanobot plugins list` | Show available channels and optional capabilities |
| `nanobot plugins enable <name>` | Install missing support and enable the feature or channel |
| `nanobot plugins enable <name> --logs` | Show package install logs while enabling |
| `nanobot plugins disable <channel>` | Turn off a channel without deleting its saved settings |
| `nanobot plugins list --config <path>` | Read a specific config file |
| `nanobot plugins enable <name> --config <path>` | Update a specific config file |
| `nanobot plugins disable <channel> --config <path>` | Turn off a channel in a specific config file |

Document and PDF reading are included in the standard installation. The old
`nanobot plugins enable documents` and `nanobot plugins enable pdf` commands
remain accepted as no-op compatibility aliases.

## Provider OAuth

| Command | Description |
|---|---|
| `nanobot provider login openai-codex --set-main` | Authenticate Codex and select its current default model |
| `nanobot provider login github-copilot --set-main` | Authenticate GitHub Copilot and select its current default model |
| `nanobot provider logout openai-codex` | Remove OpenAI Codex OAuth state |
| `nanobot provider logout github-copilot` | Remove GitHub Copilot OAuth state |

See [`providers.md`](./providers.md#oauth-providers) for when OAuth providers need explicit provider/model selection.

## Audit Evidence

`nanobot audit` reads the append-only Agent audit evidence. Commands use the selected instance's
configured `audit.path`, or its default `<config-directory>/audit/v1`. Pass `--audit-root` to inspect
an archive or copied evidence bundle directly.

```bash
# List newest Traces and continue with the returned cursor.
nanobot audit list --limit 50
nanobot audit list --limit 50 --cursor '<cursor>'

# Reconstruct a Trace. Payloads are hidden unless explicitly requested.
nanobot audit show <trace-id>
nanobot audit show <trace-id> --include-payloads

# Verify one Trace or every discovered process lineage.
nanobot audit verify <trace-id>
nanobot audit verify --all

# Export sanitized evidence, full plaintext, or a verification bundle.
nanobot audit export <trace-id> -o trace.json --mode sanitized
nanobot audit export <trace-id> -o trace-full.json --mode full
nanobot audit export <trace-id> -o trace-bundle.json --mode evidence_bundle

# Build/check the disposable index and aggregate all indexed evidence.
nanobot audit index status
nanobot audit index rebuild
nanobot audit stats --all --group-by source_type
nanobot audit doctor
```

`show` is sanitized by default. `--include-payloads` and `export --mode full` can expose permanently
stored plaintext prompts, responses, reasoning, tool inputs, and tool outputs, and therefore print
a warning. Export files are ordinary files outside the audit hash lineage; protect them separately.

`verify` exits nonzero for non-valid evidence. Verification detects damage and deletion relative to
the catalogs that remain present. Local hashes are unsigned and cannot prove non-repudiation or
detect deletion of the entire audit root. No command automatically expires or deletes evidence.

For another instance, pass `--config /path/to/config.json` to every command. `--audit-root` takes
precedence over `--config` and is useful for offline investigation:

```bash
nanobot audit verify --all --config ~/.nanobot/config.json
nanobot audit doctor --audit-root /mnt/audit-archive/v1
```

## Useful First Checks

```bash
nanobot --version
nanobot status
nanobot agent -m "Hello!"
```

If these fail, use [`troubleshooting.md`](./troubleshooting.md) before debugging WebUI, chat apps, Docker, systemd, or SDK integrations.
