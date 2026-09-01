# -*- coding: utf-8 -*-
"""span 构造层：TrialRecord → OTel span 计划 → OTel 上报。

映射关系（语义轨）
- Trial → Trace（root span "trial"）
- agent Step（含 model_name/metrics）→ Generation span "llm"
- ToolCall + Observation → Tool span "tool.<function_name>"
- user/system Step → root 上的事件（保留任务指令与环境反馈上下文）
- subagent_trajectories（ATIF v1.7）→ root 下的汇总 span

要点
- 过滤聚合属性（session/tags/experiment.*）显式打到每个 span，
  等价于官方 BaggageSpanProcessor 的传播效果（导出器自建全部 span）。
- 时间戳：root = 首末 step 区间；Generation/Tool = 当前 step 到下一 step（近似值）。
- 真实 TTFT/TPOT 需结构轨 Sidecar，本工具仅回填近似时延。
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .sanitize import sanitize


# ---------- 纯 Python 计划层（不依赖 OTel，可离线 dry-run） ----------

@dataclass
class SpanSpec:
    name: str
    role: str                     # root | generation | tool | subagent
    attributes: Dict[str, Any]
    start_ts: Optional[str] = None
    end_ts: Optional[str] = None
    parent: Optional[int] = None  # 计划内父节点下标；0 即 root
    events: List[Dict[str, Any]] = field(default_factory=list)


def _parse_ts(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _ns(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


def _derive_system(model_name: Optional[str]) -> str:
    m = (model_name or "").lower()
    for prefix in ("openai", "anthropic", "google", "gemini", "meta-llama",
                   "mistral", "qwen", "deepseek", "zhipu", "baichuan"):
        if prefix in m:
            return {"gemini": "google", "meta-llama": "meta"}.get(prefix, prefix)
    return "unknown"


def build_span_plan(trial, mode: str = "structural",
                    run_name: Optional[str] = None,
                    dataset_id: Optional[str] = None) -> List[SpanSpec]:
    steps = trial.steps
    start_ts = next((s.timestamp for s in steps if s.timestamp), None)
    end_ts = next((s.timestamp for s in reversed(steps) if s.timestamp), None)

    shared: Dict[str, Any] = {
        "langfuse.session.id": trial.session_id,
        "langfuse.trace.name": f"{trial.task_id}#{trial.attempt}",
        "langfuse.trace.tags": ["harbor", trial.agent_name, trial.model_name],
        "langfuse.experiment.id": trial.run_id,
        "langfuse.experiment.name": run_name or trial.run_id,
        "langfuse.user.id": trial.agent_name,
    }
    if dataset_id:
        shared["langfuse.experiment.dataset.id"] = dataset_id

    fm = trial.final_metrics or {}
    trace_metadata = {
        "langfuse.trace.metadata.total_prompt_tokens": fm.get("total_prompt_tokens"),
        "langfuse.trace.metadata.total_completion_tokens": fm.get("total_completion_tokens"),
        "langfuse.trace.metadata.total_cached_tokens": fm.get("total_cached_tokens"),
        "langfuse.trace.metadata.total_cost_usd": fm.get("total_cost_usd"),
        "langfuse.trace.metadata.total_steps": fm.get("total_steps"),
    }

    root_events = []
    for s in steps:
        if s.source in ("user", "system") and s.message:
            root_events.append({
                "name": f"step.{s.step_id}.{s.source}",
                "ts": s.timestamp,
                "attrs": {"message": sanitize(s.message, mode)},
            })

    root = SpanSpec(
        name="trial",
        role="root",
        attributes={
            **shared,
            **trace_metadata,
            "langfuse.observation.input": sanitize(trial.task_prompt, mode),
            "langfuse.observation.output": sanitize(trial.final_message, mode),
            "langfuse.experiment.item.id": trial.task_id,
            "atif.trial_dir": trial.trial_dir.name,
        },
        start_ts=start_ts,
        end_ts=end_ts,
        parent=None,
        events=root_events,
    )
    plan: List[SpanSpec] = [root]

    for i, s in enumerate(steps):
        next_ts = steps[i + 1].timestamp if i + 1 < len(steps) else None
        gen_idx: Optional[int] = None
        if s.source == "agent" and (s.model_name or s.metrics):
            gen_attrs: Dict[str, Any] = {
                **shared,
                "gen_ai.system": _derive_system(s.model_name),
                "gen_ai.request.model": s.model_name or "unknown",
                "langfuse.observation.input": sanitize(s.message, mode),
            }
            m = s.metrics
            if m.get("prompt_tokens") is not None:
                gen_attrs["gen_ai.usage.input_tokens"] = m["prompt_tokens"]
            if m.get("completion_tokens") is not None:
                gen_attrs["gen_ai.usage.output_tokens"] = m["completion_tokens"]
            if m.get("cached_tokens"):
                gen_attrs["gen_ai.usage.details.cached_tokens"] = m["cached_tokens"]
            if m.get("cost_usd") is not None:
                gen_attrs["atif.cost_usd"] = m["cost_usd"]
            if s.reasoning_content:
                gen_attrs["atif.reasoning"] = sanitize(s.reasoning_content, mode)
            gen_idx = len(plan)
            plan.append(SpanSpec(
                name="llm", role="generation", attributes=gen_attrs,
                start_ts=s.timestamp, end_ts=next_ts, parent=0,
            ))
        for tc in s.tool_calls or []:
            tool_attrs: Dict[str, Any] = {
                **shared,
                "langfuse.observation.input": sanitize(tc.get("arguments"), mode),
                "langfuse.observation.output": sanitize(s.observation_text, mode),
                "atif.tool_call_id": tc.get("tool_call_id"),
                "atif.model_name": s.model_name,
            }
            plan.append(SpanSpec(
                name=f"tool.{tc.get('function_name') or 'unknown'}",
                role="tool", attributes=tool_attrs,
                start_ts=s.timestamp, end_ts=next_ts,
                parent=gen_idx if gen_idx is not None else 0,
            ))

    for i, sub in enumerate(trial.subagent_trajectories or []):
        sub_agent = sub.get("agent") or {}
        sub_fm = sub.get("final_metrics") or {}
        sub_steps = sub.get("steps") or []
        plan.append(SpanSpec(
            name=f"subagent.{sub_agent.get('name') or i}",
            role="subagent",
            attributes={
                **shared,
                "atif.subagent.model": sub_agent.get("model_name") or "unknown",
                "atif.subagent.steps": len(sub_steps),
                "atif.subagent.total_prompt_tokens": sub_fm.get("total_prompt_tokens"),
                "atif.subagent.total_completion_tokens": sub_fm.get("total_completion_tokens"),
            },
            start_ts=next((st.get("timestamp") for st in sub_steps
                           if st.get("timestamp")), None),
            end_ts=next((st.get("timestamp") for st in reversed(sub_steps)
                         if st.get("timestamp")), None),
            parent=0,
        ))

    return plan


# ---------- OTel 执行层（懒加载 OTel 依赖） ----------

def _fmt_trace_id(tid: int) -> str:
    return format(tid, "032x")


def _fmt_span_id(sid: int) -> str:
    return format(sid, "016x")


def _to_attrs(attrs: Dict[str, Any]) -> Dict[str, Any]:
    """OTel 属性只接受原始类型或原始类型序列；
    dict/list（如 structural 脱敏结果）统一 JSON 序列化成字符串。"""

    def conv(v: Any) -> Any:
        if v is None or isinstance(v, (str, bool, int, float)):
            return v
        if isinstance(v, (list, tuple)) and all(
                isinstance(x, (str, bool, int, float)) for x in v):
            return list(v)
        return json.dumps(v, ensure_ascii=False)

    return {k: conv(v) for k, v in attrs.items() if v is not None}


def execute_plan(tracer, plan: List[SpanSpec]) -> Dict[str, str]:
    """把计划变成真实 OTel span 树并结束，返回 trace_id/observation_id。"""
    from opentelemetry import trace as otel_trace
    from opentelemetry.trace import NonRecordingSpan

    root_spec = plan[0]
    root = tracer.start_span(
        root_spec.name,
        attributes=_to_attrs(root_spec.attributes),
        start_time=_ns(_parse_ts(root_spec.start_ts)),
    )
    root.set_attribute(
        "langfuse.experiment.item.root_observation_id",
        _fmt_span_id(root.get_span_context().span_id),
    )
    for ev in root_spec.events:
        root.add_event(
            ev["name"],
            attributes=_to_attrs(ev.get("attrs") or {}),
            timestamp=_ns(_parse_ts(ev.get("ts"))),
        )

    contexts = {0: otel_trace.set_span_in_context(
        NonRecordingSpan(root.get_span_context()))}
    open_spans = []
    for idx, spec in enumerate(plan[1:], start=1):
        parent_ctx = contexts.get(spec.parent, contexts[0])
        span = tracer.start_span(
            spec.name,
            context=parent_ctx,
            attributes=_to_attrs(spec.attributes),
            start_time=_ns(_parse_ts(spec.start_ts)),
        )
        contexts[idx] = otel_trace.set_span_in_context(
            NonRecordingSpan(span.get_span_context()))
        open_spans.append((span, spec))

    for span, spec in reversed(open_spans):
        span.end(end_time=_ns(_parse_ts(spec.end_ts)))
    root.end(end_time=_ns(_parse_ts(root_spec.end_ts)))

    sc = root.get_span_context()
    return {"trace_id": _fmt_trace_id(sc.trace_id),
            "observation_id": _fmt_span_id(sc.span_id)}


def build_provider(otel_endpoint: str, public_key: str, secret_key: str):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    provider = TracerProvider(resource=Resource.create({
        "service.name": "atif2langfuse",
        "langfuse.environment": "experiment",
    }))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
        endpoint=otel_endpoint.rstrip("/") + "/v1/traces",
        headers={
            "Authorization": f"Basic {auth}",
            # v4 数据模型实时入库；缺省可能延迟最长 10 分钟
            "x-langfuse-ingestion-version": "4",
        },
    )))
    return provider
