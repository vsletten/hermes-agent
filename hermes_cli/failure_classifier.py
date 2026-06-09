"""Project Autopilot worker/task failure classifier.

The classifier is deterministic and side-effect free.  Kanban failure paths use
it only after a task is known to be Project Autopilot-owned so legacy boards keep
existing retry semantics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class FailureClassifierInput:
    task_id: str
    profile: str | None = None
    outcome: str | None = None
    error_text: str | None = None
    event_kind: str | None = None
    metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class FailureClassifierOutput:
    failure_class: str
    normalized_fingerprint: str
    scope: str
    confidence: float
    owner: str
    legal_next_action: str
    retry_budget_key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class,
            "normalized_fingerprint": self.normalized_fingerprint,
            "scope": self.scope,
            "confidence": self.confidence,
            "owner": self.owner,
            "legal_next_action": self.legal_next_action,
            "retry_budget_key": self.retry_budget_key,
        }


_AUTH_EXPIRED_RE = re.compile(
    r"\b(http\s*401|401\s+unauthori[sz]ed|token[_ -]?expired|oauth.*(expired|refresh)|refresh.*failed|invalid_grant)\b",
    re.IGNORECASE,
)
_MISSING_KEY_RE = re.compile(
    r"\b(missing|required|unset|not\s+set)\b.*\b(api[_ -]?key|[a-z0-9_]*_api_key|token|credential|secret)\b|\b(no\s+api[_ -]?key)\b",
    re.IGNORECASE,
)
_MODEL_AUTH_RE = re.compile(
    r"\b(model|provider|adapter|llm).*\b(unauthori[sz]ed|forbidden|auth|credential|rejected)\b|\b(unauthori[sz]ed|forbidden).*\b(model|provider|adapter|llm)\b",
    re.IGNORECASE,
)
_PROTOCOL_RE = re.compile(
    r"protocol violation|without calling\s+kanban_(complete|block)|exited cleanly.*still running",
    re.IGNORECASE,
)
_WORKSPACE_RE = re.compile(r"workspace[-_ ]preflight|declared worktree path|workspace.*(missing|does not exist|access denied)", re.IGNORECASE)
_COMPLETION_ARTIFACT_RE = re.compile(r"project-completion-contract|missing artifact|empty artifact|status report", re.IGNORECASE)
_TIMEOUT_RE = re.compile(r"\b(timed? out|timeout|max_runtime|deadline exceeded)\b", re.IGNORECASE)
_MANUAL_RE = re.compile(r"\b(manual|operator|user)\b.*\b(kill|interrupt|stop|cancel)", re.IGNORECASE)


def _budget_key(task_id: str, profile: str | None, failure_class: str, fingerprint: str) -> str:
    return f"{task_id}:{profile or 'unassigned'}:{failure_class}:{fingerprint}"


def _out(
    inp: FailureClassifierInput,
    *,
    failure_class: str,
    fingerprint: str,
    scope: str,
    confidence: float,
    owner: str,
    action: str,
) -> FailureClassifierOutput:
    return FailureClassifierOutput(
        failure_class=failure_class,
        normalized_fingerprint=fingerprint,
        scope=scope,
        confidence=confidence,
        owner=owner,
        legal_next_action=action,
        retry_budget_key=_budget_key(inp.task_id, inp.profile, failure_class, fingerprint),
    )


def classify_worker_or_task_failure(inp: FailureClassifierInput) -> FailureClassifierOutput:
    """Classify a worker/task failure into a stable project fingerprint."""

    text = " ".join(
        str(part or "")
        for part in (inp.outcome, inp.event_kind, inp.error_text, inp.metadata or "")
    )

    if _MANUAL_RE.search(text):
        return _out(
            inp,
            failure_class="operator_interrupt",
            fingerprint="manual_operator_interrupt",
            scope="task",
            confidence=0.95,
            owner="operator",
            action="record operator intent, then unblock or rerun only if still desired",
        )
    if _AUTH_EXPIRED_RE.search(text):
        return _out(
            inp,
            failure_class="worker_health",
            fingerprint="worker_auth_expired",
            scope="profile",
            confidence=0.95,
            owner="worker",
            action="repair worker profile authentication or reroute by policy before dispatch",
        )
    if _MISSING_KEY_RE.search(text):
        return _out(
            inp,
            failure_class="worker_health",
            fingerprint="worker_auth_missing_api_key",
            scope="profile",
            confidence=0.9,
            owner="worker",
            action="configure the missing worker API key/token before dispatch",
        )
    if _MODEL_AUTH_RE.search(text):
        return _out(
            inp,
            failure_class="worker_health",
            fingerprint="worker_model_auth_failed",
            scope="profile",
            confidence=0.85,
            owner="worker",
            action="repair model/provider credentials for the worker profile before dispatch",
        )
    if _PROTOCOL_RE.search(text):
        return _out(
            inp,
            failure_class="worker_protocol",
            fingerprint="worker_protocol_violation_before_work",
            scope="profile",
            confidence=0.9,
            owner="worker",
            action="fix worker protocol/tool-use path before same-profile dispatch",
        )
    if _WORKSPACE_RE.search(text):
        return _out(
            inp,
            failure_class="process_preflight",
            fingerprint="workspace_preflight_failed",
            scope="task",
            confidence=0.9,
            owner="process",
            action="repair declared workspace or task contract before dispatch",
        )
    if _COMPLETION_ARTIFACT_RE.search(text):
        return _out(
            inp,
            failure_class="completion_contract",
            fingerprint="completion_artifact_missing",
            scope="task",
            confidence=0.85,
            owner="process",
            action="produce required artifact/status report before completing task",
        )
    if _TIMEOUT_RE.search(text):
        return _out(
            inp,
            failure_class="task_runtime",
            fingerprint="task_timeout",
            scope="task",
            confidence=0.8,
            owner="process",
            action="inspect timeout cause and adjust task/runtime before retry",
        )

    return _out(
        inp,
        failure_class="unknown_failure",
        fingerprint="unknown_failure",
        scope="task",
        confidence=0.2,
        owner="process",
        action="inspect failure logs and classify before retrying project task",
    )
