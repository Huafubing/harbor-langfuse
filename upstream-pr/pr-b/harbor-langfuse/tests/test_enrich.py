from harbor_langfuse.enrich import enrich_resource_spans
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans


def _mk_spans(n=2):
    rs = ResourceSpans()
    scope = rs.scope_spans.add()
    spans = [scope.spans.add() for _ in range(n)]
    return rs, spans


def _get(span, key):
    for kv in span.attributes:
        if kv.key == key:
            return kv
    return None


def test_adds_attributes_to_every_span():
    rs, spans = _mk_spans(3)
    enrich_resource_spans(
        rs,
        session_id="sess-1",
        trace_name="task#1",
        tags=["harbor", "agent", "model"],
        experiment_id="exp-1",
        experiment_name="nightly",
        dataset_id="ds-1",
        item_id="hello-world",
        user_id="agent",
        release="v1",
    )
    for span in spans:
        assert _get(span, "langfuse.session.id").value.string_value == "sess-1"
        assert _get(span, "langfuse.trace.name").value.string_value == "task#1"
        tags_kv = _get(span, "langfuse.trace.tags")
        assert [v.string_value for v in tags_kv.value.array_value.values] == [
            "harbor", "agent", "model",
        ]
        assert _get(span, "langfuse.experiment.id").value.string_value == "exp-1"
        assert _get(span, "langfuse.experiment.name").value.string_value == "nightly"
        assert _get(span, "langfuse.experiment.dataset.id").value.string_value == "ds-1"
        assert _get(span, "langfuse.experiment.item.id").value.string_value == "hello-world"
        assert _get(span, "langfuse.user.id").value.string_value == "agent"
        assert _get(span, "langfuse.release").value.string_value == "v1"


def test_idempotent_without_overwrite():
    rs, spans = _mk_spans(1)
    enrich_resource_spans(rs, session_id="first")
    enrich_resource_spans(rs, session_id="second", experiment_id="exp")
    kv = _get(spans[0], "langfuse.session.id")
    assert kv.value.string_value == "first"
    count = sum(1 for kv in spans[0].attributes if kv.key == "langfuse.session.id")
    assert count == 1
    assert _get(spans[0], "langfuse.experiment.id").value.string_value == "exp"


def test_overwrite_replaces_existing():
    rs, spans = _mk_spans(1)
    enrich_resource_spans(rs, session_id="first")
    enrich_resource_spans(rs, session_id="second", overwrite=True)
    assert _get(spans[0], "langfuse.session.id").value.string_value == "second"


def test_none_values_are_skipped():
    rs, spans = _mk_spans(1)
    enrich_resource_spans(rs, session_id="s", trace_name=None)
    keys = {kv.key for kv in spans[0].attributes}
    assert "langfuse.session.id" in keys
    assert "langfuse.trace.name" not in keys
