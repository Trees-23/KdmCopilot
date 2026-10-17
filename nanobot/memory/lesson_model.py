"""Tool-free offline model calls use the existing typed audit lifecycle."""

from __future__ import annotations

from typing import Any

from nanobot.agent.hook import AgentHookContext, AgentRunHookContext, ModelRequestSnapshot
from nanobot.audit.boundaries import TurnAuditRecorder
from nanobot.audit.context import TraceTurnInput
from nanobot.audit.hook import RunnerAuditHook
from nanobot.audit.ids import new_audit_id
from nanobot.audit.runtime import AuditRuntime
from nanobot.bus.events import InboundMessage
from nanobot.providers.base import GenerationSettings
from nanobot.utils.llm_runtime import LLMRuntime


async def abstract_lesson(*, provider: Any, model: str, system: str, user: str,
                          workspace: str, input_version: str, audit_runtime: Any = None) -> tuple[str, str, str]:
    audit = audit_runtime or AuditRuntime.disabled()
    await audit.ensure_started()
    session = "asset-lesson:" + input_version
    turn = audit.context_resolver.resolve_turn(TraceTurnInput(
        session_key=session, source_type="asset_lesson", actor_type="system",
    ))
    recorder = TurnAuditRecorder(audit.emitter, turn)
    await recorder.started(InboundMessage(channel="system", sender_id="asset_lesson", chat_id=session,
                                         content="离线L2抽象；输入版本 " + input_version))
    run = turn.new_run(source_type="asset_lesson", source_metadata={"workspace": workspace, "input_version": input_version})
    runtime = LLMRuntime(provider=provider, model=model,
                         generation=GenerationSettings(temperature=0, max_tokens=1000, reasoning_effort="low"),
                         context_window_tokens=0)
    hook = RunnerAuditHook(audit.emitter, run, runtime=runtime, session_key=session)
    run_context = AgentRunHookContext(messages=[])
    await hook.before_run(run_context)
    outputs = []
    try:
        for index in range(2):
            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            context = AgentHookContext(iteration=index, messages=messages, model_call_id=new_audit_id(), session_key=session)
            await hook.before_iteration(context)
            await hook.before_model_request(context, ModelRequestSnapshot(
                model_call_id=context.model_call_id, messages=messages, tools=[], runtime=runtime,
                context_cache={"lesson_input_version": input_version, "lesson_sample_round": index + 1},
            ))
            try:
                response = await provider.chat_with_retry(
                    messages=messages, tools=None, model=model, temperature=0, max_tokens=1000,
                    reasoning_effort="low", attempt_observer=context.provider_attempt_observer,
                )
            except BaseException as error:
                await hook.on_model_request_error(context, error)
                raise
            await hook.after_model_response(context, response)
            if response.finish_reason == "error" or response.has_tool_calls or not response.content:
                raise ValueError("lesson model response unusable")
            outputs.append(response.content)
            context.stop_reason = "completed"
            await hook.after_iteration(context)
        run_context.stop_reason = "completed"
        return outputs[0], outputs[1], turn.trace_id
    except BaseException:
        run_context.stop_reason = "error"
        raise
    finally:
        await hook.on_finally(run_context)
        await recorder.finished(status="command_completed" if run_context.stop_reason == "completed" else "failed")
