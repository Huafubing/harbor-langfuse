"""Verifier rewards → Langfuse Scores.

Score bodies follow Langfuse's ``CreateScoreRequest`` (``POST /scores``):
numeric values are passed as numbers with ``dataType: "NUMERIC"``,
categorical values as strings with ``dataType: "CATEGORICAL"``. Scores are
upserted by their ``id``; deterministic (UUIDv5) ids make re-running a trial
or re-posting scores idempotent.
"""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .api import LangfuseRestClient


def stable_score_id(scope: str, key: str) -> str:
    """Deterministic, idempotent score id scoped to one trial."""
    return str(uuid5(NAMESPACE_URL, f"{scope}:{key}"))


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def build_score_items(
    rewards: dict[str, Any] | None,
    *,
    trace_id: str | None = None,
    observation_id: str | None = None,
    session_id: str | None = None,
    id_scope: str | None = None,
    comment: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build ``CreateScoreRequest`` bodies from a verifier rewards mapping.

    Each reward key becomes one score with its name kept verbatim (mirroring
    the ``LangSmithPlugin`` feedback precedent). Numeric values map to
    ``NUMERIC``, strings to ``CATEGORICAL``.
    """
    items: list[dict[str, Any]] = []
    scope = id_scope or trace_id or session_id or "harbor"
    for key, value in (rewards or {}).items():
        item: dict[str, Any] = {
            "id": stable_score_id(scope, str(key)),
            "name": str(key),
            "value": value if _is_number(value) else str(value),
            "dataType": "NUMERIC" if _is_number(value) else "CATEGORICAL",
            "metadata": {"source": "harbor", **(metadata or {})},
        }
        if trace_id is not None:
            item["traceId"] = trace_id
        if observation_id is not None:
            item["observationId"] = observation_id
        if session_id is not None:
            item["sessionId"] = session_id
        if comment is not None:
            item["comment"] = comment
        items.append(item)
    return items


def build_exception_score(
    *,
    trace_id: str | None = None,
    observation_id: str | None = None,
    session_id: str | None = None,
    id_scope: str | None = None,
    name: str = "harbor_error",
    message: str | None = None,
) -> dict[str, Any]:
    """Boolean score marking a trial that failed with an exception (1 = errored)."""
    scope = id_scope or trace_id or session_id or "harbor"
    item: dict[str, Any] = {
        "id": stable_score_id(scope, name),
        "name": name,
        "value": 1.0,
        "dataType": "BOOLEAN",
        "metadata": {"source": "harbor"},
    }
    if trace_id is not None:
        item["traceId"] = trace_id
    if observation_id is not None:
        item["observationId"] = observation_id
    if session_id is not None:
        item["sessionId"] = session_id
    if message:
        item["comment"] = message[:500]
    return item


def post_scores(
    client: LangfuseRestClient,
    items: list[dict[str, Any]],
) -> tuple[int, int]:
    """POST each score item. Returns ``(ok_count, failed_count)``.

    Individual failures are logged and counted, not raised — a broken score
    must not fail an otherwise successful eval job (unless ``fail_fast``).
    """
    ok = 0
    failed = 0
    for item in items:
        try:
            client.request("POST", "/scores", ok_statuses=(200, 201), json=item)
            ok += 1
        except Exception:
            logger = __import__("logging").getLogger(__name__)
            logger.warning("LangfusePlugin: failed to post score %s", item.get("name"), exc_info=True)
            failed += 1
    return ok, failed
