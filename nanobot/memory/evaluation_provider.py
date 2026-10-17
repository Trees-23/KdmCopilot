"""Per-evaluation Provider wrapper that charges each observed leaf request."""

from __future__ import annotations

from typing import Any

from nanobot.memory.evaluation_budget import EvaluationBudget
from nanobot.providers.base import LLMProvider
from nanobot.providers.observed_call import ProviderCallAdmissionError, notify_observer


class EvaluationExecutionError(RuntimeError):
    """A Provider execution failure, distinct from a scored candidate rejection."""

    def __init__(self, *, retryable: bool) -> None:
        super().__init__("evaluation_provider_error")
        self.retryable = retryable


class _BudgetObserver:
    def __init__(self, budget: EvaluationBudget, downstream: Any) -> None:
        self.budget = budget
        self.downstream = downstream
        self.rejected: ProviderCallAdmissionError | None = None

    async def attempt_started(self, snapshot: Any) -> None:
        try:
            self.budget.charge_request()
        except ProviderCallAdmissionError as error:
            self.rejected = error
            raise
        await notify_observer(self.downstream, "attempt_started", snapshot)

    async def attempt_finished(self, result: Any) -> None:
        await notify_observer(self.downstream, "attempt_finished", result)

    async def route_decision(self, decision: Any) -> None:
        await notify_observer(self.downstream, "route_decision", decision)

    async def retry_scheduled(self, retry: Any) -> None:
        await notify_observer(self.downstream, "retry_scheduled", retry)


class EvaluationProvider:
    """Delegate without mutating the Gateway's shared Provider or generation settings."""

    def __init__(self, provider: Any, budget: EvaluationBudget) -> None:
        self._provider = provider
        self._budget = budget
        self.rejected_error: ProviderCallAdmissionError | None = None
        self.execution_error: EvaluationExecutionError | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    async def _call(self, method: str, kwargs: dict[str, Any]) -> Any:
        observer = _BudgetObserver(self._budget, kwargs.get("attempt_observer"))
        kwargs["attempt_observer"] = observer
        try:
            response = await getattr(self._provider, method)(**kwargs)
        except ProviderCallAdmissionError as error:
            self.rejected_error = error
            raise
        # Provider-specific exception adapters may turn a refused dispatch into
        # an error response. It must never become a scored evaluation answer.
        if observer.rejected is not None:
            self.rejected_error = observer.rejected
            raise observer.rejected
        if response.finish_reason == "error":
            self.execution_error = EvaluationExecutionError(retryable=LLMProvider._is_transient_response(response))
            raise self.execution_error
        return response

    async def chat_with_retry(self, **kwargs: Any) -> Any:
        return await self._call("chat_with_retry", kwargs)

    async def chat_stream_with_retry(self, **kwargs: Any) -> Any:
        return await self._call("chat_stream_with_retry", kwargs)
