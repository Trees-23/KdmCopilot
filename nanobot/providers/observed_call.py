"""Typed observation around each real Provider API call."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from loguru import logger

from nanobot.audit.ids import new_audit_id
from nanobot.providers.base import LLMResponse


class ProviderCallAdmissionError(RuntimeError):
    """An authoritative pre-dispatch guard rejected this model request."""


@dataclass(frozen=True, slots=True)
class ProviderAttemptSnapshot:
    attempt_id: str
    attempt_ordinal: int
    provider: str
    model: str
    input_variant: str
    started_monotonic_ns: int


@dataclass(frozen=True, slots=True)
class ProviderAttemptResult:
    attempt_id: str
    attempt_ordinal: int
    provider: str
    model: str
    input_variant: str
    elapsed_ms: int
    status: str
    error_kind: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderRouteDecision:
    action: str
    provider: str
    model: str
    input_variant: str


@dataclass(frozen=True, slots=True)
class ProviderRetryDecision:
    prior_attempt_id: str | None
    delay_ms: int
    policy_name: str


class ProviderAttemptObserver(Protocol):
    async def attempt_started(self, snapshot: ProviderAttemptSnapshot) -> None: ...

    async def attempt_finished(self, snapshot: ProviderAttemptResult) -> None: ...

    async def route_decision(self, decision: ProviderRouteDecision) -> None: ...

    async def retry_scheduled(self, retry: ProviderRetryDecision) -> None: ...


@dataclass(frozen=True, slots=True)
class ObservedProviderResult:
    response: LLMResponse
    attempt_id: str


async def notify_observer(
    observer: ProviderAttemptObserver | None,
    method: str,
    value: Any,
) -> None:
    if observer is None:
        return
    try:
        await getattr(observer, method)(value)
    except ProviderCallAdmissionError:
        if method == "attempt_started":
            raise
    except Exception as error:
        logger.warning(
            "Provider attempt observer callback failed method={} kind={}",
            method,
            type(error).__name__,
        )


async def observed_provider_call(
    call: Callable[..., Awaitable[LLMResponse]],
    kwargs: dict[str, Any],
    *,
    observer: ProviderAttemptObserver | None,
    provider: str,
    model: str,
    attempt_ordinal: int,
    input_variant: str,
) -> ObservedProviderResult:
    attempt_id = new_audit_id()
    started = time.monotonic_ns()
    await notify_observer(
        observer,
        "attempt_started",
        ProviderAttemptSnapshot(
            attempt_id,
            attempt_ordinal,
            provider,
            model,
            input_variant,
            started,
        ),
    )
    try:
        response = await call(**kwargs)
    except asyncio.CancelledError:
        await notify_observer(
            observer,
            "attempt_finished",
            ProviderAttemptResult(
                attempt_id,
                attempt_ordinal,
                provider,
                model,
                input_variant,
                max(0, (time.monotonic_ns() - started) // 1_000_000),
                "cancelled",
                "CancelledError",
            ),
        )
        raise
    except Exception as error:
        await notify_observer(
            observer,
            "attempt_finished",
            ProviderAttemptResult(
                attempt_id,
                attempt_ordinal,
                provider,
                model,
                input_variant,
                max(0, (time.monotonic_ns() - started) // 1_000_000),
                "error",
                type(error).__name__,
            ),
        )
        raise
    if response.finish_reason == "error":
        status = "timeout" if response.error_kind == "timeout" else "error"
        error_kind = response.error_kind or response.error_type or "provider_error"
    else:
        status, error_kind = "ok", None
    await notify_observer(
        observer,
        "attempt_finished",
        ProviderAttemptResult(
            attempt_id,
            attempt_ordinal,
            provider,
            model,
            input_variant,
            max(0, (time.monotonic_ns() - started) // 1_000_000),
            status,
            error_kind,
        ),
    )
    return ObservedProviderResult(response, attempt_id)
