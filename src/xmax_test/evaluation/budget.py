"""Persistent paid-evaluation budget policy and gate.

The provider reserves a conservative maximum before a paid request.  A
successful response settles that reservation from the provider's actual usage;
an ambiguous timeout forfeits the full reservation.  This makes the configured
limit a hard local ceiling even across multiple workers or abrupt restarts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from typing import Any

from ..errors import ConfigError, EvaluationBudgetPausedError

MICROS_PER_CNY = Decimal("1000000")
TOKENS_PER_MILLION = Decimal("1000000")


def cny_to_micros(value: Any) -> int:
    amount = Decimal(str(value))
    if amount < 0:
        raise ConfigError("evaluation budget amounts cannot be negative")
    return int((amount * MICROS_PER_CNY).to_integral_value(rounding=ROUND_CEILING))


def micros_to_cny(value: int) -> float:
    return float(Decimal(int(value)) / MICROS_PER_CNY)


@dataclass(frozen=True)
class PriceTier:
    max_input_tokens: int
    input_cny_per_million: Decimal
    cached_input_cny_per_million: Decimal
    output_cny_per_million: Decimal


@dataclass(frozen=True)
class PaidFallbackPolicy:
    budget_id: str
    provider_id: str
    model: str
    currency: str
    limit_micros: int
    reservation_micros: int
    tiers: tuple[PriceTier, ...]

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> PaidFallbackPolicy | None:
        source = dict(config or {})
        if not source or not source.get("enabled", False):
            return None
        tiers = tuple(
            PriceTier(
                max_input_tokens=int(item["max_input_tokens"]),
                input_cny_per_million=Decimal(str(item["input_cny_per_million"])),
                cached_input_cny_per_million=Decimal(
                    str(item.get("cached_input_cny_per_million", item["input_cny_per_million"]))
                ),
                output_cny_per_million=Decimal(str(item["output_cny_per_million"])),
            )
            for item in source.get("pricing_tiers", [])
        )
        if not tiers:
            raise ConfigError("paid_fallback requires at least one pricing tier")
        ordered = tuple(sorted(tiers, key=lambda item: item.max_input_tokens))
        if ordered != tiers or len({item.max_input_tokens for item in tiers}) != len(tiers):
            raise ConfigError("paid_fallback pricing tiers must be unique and ascending")
        model = str(source.get("model") or "")
        if not model:
            raise ConfigError("paid_fallback.model is required")
        currency = str(source.get("currency", "CNY"))
        if currency != "CNY":
            raise ConfigError("only CNY evaluation budgets are currently supported")
        limit_micros = cny_to_micros(source.get("limit_cny", 0))
        reservation_micros = cny_to_micros(source.get("max_reservation_cny", 0))
        if limit_micros <= 0:
            raise ConfigError("paid_fallback.limit_cny must be greater than zero")
        if reservation_micros <= 0 or reservation_micros > limit_micros:
            raise ConfigError(
                "paid_fallback.max_reservation_cny must be greater than zero and not exceed the limit"
            )
        return cls(
            budget_id=str(source.get("budget_id") or f"{model}-paid"),
            provider_id=str(source.get("provider_id") or "openai_compatible"),
            model=model,
            currency=currency,
            limit_micros=limit_micros,
            reservation_micros=reservation_micros,
            tiers=tiers,
        )

    def estimate_usage_micros(self, usage: dict[str, Any]) -> int:
        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion_tokens = int(
            usage.get("completion_tokens") or usage.get("output_tokens") or 0
        )
        details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
        cached_tokens = min(prompt_tokens, int(details.get("cached_tokens") or 0))
        uncached_tokens = max(0, prompt_tokens - cached_tokens)
        if prompt_tokens > self.tiers[-1].max_input_tokens:
            raise ConfigError(
                f"provider usage input_tokens={prompt_tokens} exceeds configured pricing tiers"
            )
        tier = next(
            (item for item in self.tiers if prompt_tokens <= item.max_input_tokens),
            self.tiers[-1],
        )
        cost = (
            Decimal(uncached_tokens) * tier.input_cny_per_million
            + Decimal(cached_tokens) * tier.cached_input_cny_per_million
            + Decimal(completion_tokens) * tier.output_cny_per_million
        ) / TOKENS_PER_MILLION
        return int((cost * MICROS_PER_CNY).to_integral_value(rounding=ROUND_CEILING))


class EvaluationBudgetGate:
    """Coordinates evaluation admission and paid-call reservations."""

    def __init__(self, repository: Any, policy: PaidFallbackPolicy) -> None:
        self._repository = repository
        self.policy = policy
        self._repository.ensure_evaluation_budget(
            budget_id=policy.budget_id,
            provider_id=policy.provider_id,
            model=policy.model,
            currency=policy.currency,
            limit_micros=policy.limit_micros,
        )

    def status(self) -> dict[str, Any]:
        return self._repository.get_evaluation_budget(self.policy.budget_id)

    def assert_evaluation_allowed(self) -> None:
        state = self.status()
        if state["status"] == "paused":
            raise self._paused_error(state)

    def reserve_paid_call(self) -> dict[str, Any]:
        state = self.status()
        if state["status"] == "awaiting_authorization":
            state = self.pause(
                "paid qwen3-vl-flash fallback reached before explicit recharge authorization"
            )
            raise self._paused_error(state)
        try:
            return self._repository.reserve_evaluation_budget(
                self.policy.budget_id,
                self.policy.reservation_micros,
            )
        except EvaluationBudgetPausedError:
            raise

    def settle(self, reservation_id: str, usage: dict[str, Any]) -> dict[str, Any]:
        actual = self.policy.estimate_usage_micros(usage)
        return self._repository.settle_evaluation_budget_reservation(
            reservation_id,
            actual_micros=actual,
            usage=usage,
        )

    def release(self, reservation_id: str, *, reason: str) -> dict[str, Any]:
        return self._repository.release_evaluation_budget_reservation(
            reservation_id, reason=reason
        )

    def forfeit(self, reservation_id: str, *, reason: str) -> dict[str, Any]:
        return self._repository.forfeit_evaluation_budget_reservation(
            reservation_id, reason=reason
        )

    def pause(self, reason: str) -> dict[str, Any]:
        return self._repository.pause_evaluation_budget(self.policy.budget_id, reason=reason)

    def authorize(self, *, operator: str, limit_cny: Any | None = None) -> dict[str, Any]:
        limit_micros = (
            self.policy.limit_micros if limit_cny is None else cny_to_micros(limit_cny)
        )
        return self._repository.authorize_evaluation_budget(
            self.policy.budget_id,
            limit_micros=limit_micros,
            operator=operator,
        )

    @staticmethod
    def _paused_error(state: dict[str, Any]) -> EvaluationBudgetPausedError:
        return EvaluationBudgetPausedError(
            "MLLM evaluation is paused before all Judges: "
            f"budget={state['budget_id']} status={state['status']} "
            f"spent_cny={state['spent_cny']:.6f} reserved_cny={state['reserved_cny']:.6f} "
            f"limit_cny={state['limit_cny']:.2f} reason={state.get('paused_reason') or 'none'}; "
            "after confirming the Alibaba recharge and qwen3-vl-flash paid access, run "
            f"`xmax-test evaluation-budget authorize --budget-id {state['budget_id']} "
            "--limit-cny 99 --operator <name> --recharge-confirmed`"
        )
