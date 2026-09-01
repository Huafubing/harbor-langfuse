# -*- coding: utf-8 -*-
"""离线验证 OTel 执行链路：InMemorySpanExporter 断言 span 树结构与属性。

运行：cd /root/harbor-langfuse && python3 tests/test_execute_plan.py
不依赖网络与 Langfuse 实例。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from atif2langfuse.reader import load_trials
from atif2langfuse.scores import _score_items
from atif2langfuse.spans import build_span_plan, execute_plan


def main() -> int:
    trials, _ = load_trials("examples/demo-run")
    assert len(trials) == 1, "应发现 1 个 Trial"
    trial = trials[0]

    plan = build_span_plan(trial, mode="structural", run_name="demo-run")
    roles = {}
    for spec in plan:
        roles[spec.role] = roles.get(spec.role, 0) + 1
    assert roles == {"root": 1, "generation": 2, "tool": 1}, roles

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")

    ids = execute_plan(tracer, plan)
    assert len(ids["trace_id"]) == 32 and len(ids["observation_id"]) == 16

    spans = exporter.get_finished_spans()
    assert len(spans) == 4, f"span 数应为 4，实际 {len(spans)}"

    root = next(s for s in spans if s.name == "trial")
    assert root.parent is None, "root 不应有父 span"
    children_of_root = [s for s in spans
                        if s.parent is not None
                        and s.parent.span_id == root.context.span_id]
    assert len(children_of_root) == 2, "root 应有 2 个直接子 span（2 个 generation）"
    tool = next(s for s in spans if s.name == "tool.file_write")
    gen = next(s for s in spans
               if s.name == "llm"
               and s.context.span_id == tool.parent.span_id)
    assert gen.parent.span_id == root.context.span_id, \
        "tool 的父 generation 应挂在 root 下"
    assert root.attributes.get("langfuse.session.id") == "demo-session-0001"
    assert root.attributes.get("langfuse.experiment.item.root_observation_id")
    import json as _json
    assert _json.loads(root.attributes["langfuse.observation.input"]) == {
        "_len": 44, "_sha256_8": "59578586"}
    assert root.start_time is not None and root.end_time is not None

    items = _score_items(trial)
    names = [i["name"] for i in items]
    assert "reward" in names and "reward.schema" in names, names
    assert all(i["dataType"] in ("NUMERIC", "CATEGORICAL") for i in items)

    print("trace_id:", ids["trace_id"])
    for s in spans:
        print(f"  {s.name:16s} start={s.start_time is not None} "
              f"end={s.end_time is not None}")
    print("EXECUTE_PLAN_TEST_PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
