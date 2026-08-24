"""Stable error codes and exception types.

Exit codes follow the implementation contract:

- 0 success
- 2 input/contract error
- 3 missing external dependency
- 4 external service failure
- 5 partial completion
- 6 paid approval required
- 10 internal error

Every error item serialized to ``--json`` output carries at least
``code``, ``message``, ``stage`` and ``retryable``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

EXIT_OK = 0
EXIT_INPUT_ERROR = 2
EXIT_MISSING_DEPENDENCY = 3
EXIT_EXTERNAL_FAILURE = 4
EXIT_PARTIAL = 5
EXIT_APPROVAL_REQUIRED = 6
EXIT_INTERNAL = 10


class XmaxTestError(Exception):
    """Base class for all stable project errors."""

    exit_code = EXIT_INPUT_ERROR
    code = "xmax.error"
    retryable = False
    stage = "general"

    def __init__(self, message: str, *, entity_id: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.entity_id = entity_id

    def to_dict(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "stage": self.stage,
            "retryable": self.retryable,
        }
        if self.entity_id is not None:
            item["entity_id"] = self.entity_id
        return item


class ContractError(XmaxTestError):
    """Input violates a documented contract or JSON Schema."""

    code = "xmax.contract_error"
    exit_code = EXIT_INPUT_ERROR
    stage = "contract"


class MissingInputError(ContractError):
    """Required upstream input is missing; never silently run other stages."""

    code = "xmax.missing_input"
    stage = "dependency"

    def __init__(
        self,
        message: str,
        *,
        entity_id: str | None = None,
        suggested_command: str | None = None,
    ) -> None:
        super().__init__(message, entity_id=entity_id)
        self.suggested_command = suggested_command

    def to_dict(self) -> dict[str, Any]:
        item = super().to_dict()
        if self.suggested_command is not None:
            item["suggested_command"] = self.suggested_command
        return item


class ConfigError(ContractError):
    """Configuration file is missing, invalid or refers to unknown fields."""

    code = "xmax.config_error"
    stage = "config"


class MissingDependencyError(XmaxTestError):
    """A required external capability (binary, key, plugin) is unavailable."""

    code = "xmax.missing_dependency"
    exit_code = EXIT_MISSING_DEPENDENCY
    retryable = False
    stage = "dependency"


class ExternalServiceError(XmaxTestError):
    """An external service failed after bounded retries."""

    code = "xmax.external_failure"
    exit_code = EXIT_EXTERNAL_FAILURE
    retryable = True
    stage = "external"


class RealtimeUnavailableError(XmaxTestError):
    """A realtime case cannot run because the harness rejects its inputs.

    Raised by the generate executor when the browser SDK refuses a realtime
    case (e.g. unsupported media MIME type).  Unlike a transient external
    failure it must NOT trip the streaming circuit breaker: the remaining
    offline plan is still valid and should keep draining.  The pipeline
    records the case as a generation error and continues.
    """

    code = "xmax.realtime_unavailable"
    exit_code = EXIT_PARTIAL
    retryable = False
    stage = "generate"


class PartialCompletionError(XmaxTestError):
    """Some items completed but others failed; see errors for details."""

    code = "xmax.partial"
    exit_code = EXIT_PARTIAL
    retryable = True
    stage = "pipeline"

    def __init__(self, message: str, *, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.errors = errors or []

    def to_dict(self) -> dict[str, Any]:
        item = super().to_dict()
        if self.errors:
            item["errors"] = self.errors
        return item


class ApprovalRequiredError(XmaxTestError):
    """A billed batch needs an explicit operator approval."""

    code = "xmax.approval_required"
    exit_code = EXIT_APPROVAL_REQUIRED
    retryable = False
    stage = "approval"


class EvaluationBudgetPausedError(ApprovalRequiredError):
    """Evaluation is paused before any Judge because its paid budget is closed."""

    code = "xmax.evaluation_budget_paused"
    retryable = True
    stage = "evaluate"


class EvaluationInfrastructurePausedError(ExternalServiceError):
    """Evaluation stopped globally after a bounded provider-side failure.

    This is distinct from the paid-budget gate: retrying does not require a
    recharge authorization, but the operator should first verify that the
    provider/network condition has recovered.  Orchestrators must treat it as
    a batch pause rather than manufacturing one error per remaining Case.
    """

    code = "xmax.evaluation_infrastructure_paused"
    retryable = True
    stage = "evaluate"


class MlmmTransportError(EvaluationInfrastructurePausedError):
    """A retryable MLLM transport failure exhausted provider-level retries."""

    code = "xmax.mlmm_transport_exhausted"


class MlmmRateLimitError(MlmmTransportError):
    """The provider kept rate-limiting the same model after bounded backoff."""

    code = "xmax.mlmm_rate_limited"


class MlmmAuthenticationError(EvaluationInfrastructurePausedError):
    """Provider credentials or account authorization are invalid."""

    code = "xmax.mlmm_authentication_failed"
    retryable = False


class MlmmQuotaSafetyError(EvaluationInfrastructurePausedError):
    """A quota-like response was not safe to interpret as free-tier exhaustion."""

    code = "xmax.mlmm_quota_review_required"
    retryable = False


class MlmmInvalidRequestError(ExternalServiceError):
    """The provider rejected the business input; retrying it unchanged is unsafe."""

    code = "xmax.mlmm_invalid_request"
    retryable = False
    stage = "evaluate"


class MlmmTimeoutError(EvaluationInfrastructurePausedError):
    """One MLLM request reached its bounded transport timeout."""

    code = "xmax.mlmm_timeout"
    retryable = True
    stage = "evaluate"


class StateError(ContractError):
    """An entity state transition is illegal (e.g. terminal back to running)."""

    code = "xmax.state_error"
    stage = "state"


class DuplicateError(ContractError):
    """An idempotency key already exists with a different payload."""

    code = "xmax.duplicate"
    stage = "storage"


class NotFoundError(XmaxTestError):
    """A stored entity does not exist."""

    code = "xmax.not_found"
    exit_code = EXIT_INPUT_ERROR
    stage = "storage"


class ConflictError(XmaxTestError):
    """A concurrent write or version conflict was detected."""

    code = "xmax.conflict"
    exit_code = EXIT_INPUT_ERROR
    retryable = True
    stage = "storage"


class ValidationError(ContractError):
    """Derived data failed media, schema or content validation."""

    code = "xmax.validation"
    stage = "validation"


@dataclass
class ErrorReport:
    """Aggregated error report used by context-check and CLI helpers."""

    errors: list[dict[str, Any]] = field(default_factory=list)

    def add(self, error: XmaxTestError) -> None:
        self.errors.append(error.to_dict())

    def add_item(self, item: dict[str, Any]) -> None:
        self.errors.append(item)

    def extend(self, items: list[dict[str, Any]]) -> None:
        self.errors.extend(items)

    def as_dict(self) -> dict[str, Any]:
        return {"errors": self.errors}

    def is_empty(self) -> bool:
        return not self.errors
