"""Augment converted ATIF→OTel spans with Langfuse filterable attributes.

Langfuse's OTel ingestion natively understands OpenInference attributes
(``openinference.span.kind``, ``llm.token_count.*``, ``input.value``/``output.value``).
What it additionally aggregates and filters on are attributes inside the
``langfuse.*`` namespace, which ``harbor-atif2otel.convert_trajectory()``
does not emit. Per Langfuse's documented propagation guidance, these
trace-level attributes must be present on every span in the trace —
aggregations operate across observations, not only at the root.

This module adds those attributes to every span of a ``ResourceSpans``
object in place, so the result can flow through any OTLP uploader.
"""

from __future__ import annotations

from typing import Any, Iterable

from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans

SESSION_ID = "langfuse.session.id"
TRACE_NAME = "langfuse.trace.name"
TRACE_TAGS = "langfuse.trace.tags"
EXPERIMENT_ID = "langfuse.experiment.id"
EXPERIMENT_NAME = "langfuse.experiment.name"
EXPERIMENT_DATASET_ID = "langfuse.experiment.dataset.id"
EXPERIMENT_ITEM_ID = "langfuse.experiment.item.id"
USER_ID = "langfuse.user.id"
RELEASE = "langfuse.release"


def _any_value(value: Any) -> AnyValue:
    av = AnyValue()
    if isinstance(value, bool):
        av.bool_value = value
    elif isinstance(value, int):
        av.int_value = value
    elif isinstance(value, float):
        av.double_value = value
    elif isinstance(value, str):
        av.string_value = value
    elif isinstance(value, (list, tuple)):
        for item in value:
            av.array_value.values.append(_any_value(item))
    else:
        av.string_value = str(value)
    return av


def _iter_spans(resource_spans: ResourceSpans) -> Iterable:
    for resource in resource_spans.scope_spans:
        for span in resource.spans:
            yield span


def _set_attr(attributes, key: str, value: Any, overwrite: bool) -> None:
    for kv in attributes:
        if kv.key == key:
            if overwrite:
                kv.value.CopyFrom(_any_value(value))
            return
    kv = attributes.add()
    kv.key = key
    kv.value.CopyFrom(_any_value(value))


def enrich_resource_spans(
    resource_spans: ResourceSpans,
    *,
    session_id: str | None = None,
    trace_name: str | None = None,
    tags: list[str] | tuple[str, ...] | None = None,
    experiment_id: str | None = None,
    experiment_name: str | None = None,
    dataset_id: str | None = None,
    item_id: str | None = None,
    user_id: str | None = None,
    release: str | None = None,
    overwrite: bool = False,
) -> ResourceSpans:
    """Add ``langfuse.*`` attributes to every span of ``resource_spans``.

    The mutation is applied in place and the same object is returned for
    chaining. Existing keys are preserved unless ``overwrite=True``.
    """
    values: list[tuple[str, Any]] = []
    if session_id is not None:
        values.append((SESSION_ID, session_id))
    if trace_name is not None:
        values.append((TRACE_NAME, trace_name))
    if tags is not None:
        values.append((TRACE_TAGS, list(tags)))
    if experiment_id is not None:
        values.append((EXPERIMENT_ID, experiment_id))
    if experiment_name is not None:
        values.append((EXPERIMENT_NAME, experiment_name))
    if dataset_id is not None:
        values.append((EXPERIMENT_DATASET_ID, dataset_id))
    if item_id is not None:
        values.append((EXPERIMENT_ITEM_ID, item_id))
    if user_id is not None:
        values.append((USER_ID, user_id))
    if release is not None:
        values.append((RELEASE, release))

    for span in _iter_spans(resource_spans):
        for key, value in values:
            _set_attr(span.attributes, key, value, overwrite)
    return resource_spans
