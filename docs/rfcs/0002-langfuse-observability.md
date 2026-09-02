# RFC 0002: Langfuse Observability for Harbor Jobs — Plugin Integration and Model-Path Telemetry

- **Status**: Draft (not yet submitted upstream)
- **Author**: Libotry
- **Target repository**: `harbor-framework/harbor`
- **Related**: RFC 0001 (ATIF), `packages/harbor-atif2otel`, `packages/harbor-langsmith`
- **Supersedes**: the Langfuse-uploader portion of the author's earlier draft proposal

---

## 1. Summary

This RFC proposes first-class [Langfuse](https://langfuse.com) observability for Harbor, in three composable parts:

1. **`LangfuseUploader`** (extends `packages/harbor-atif2otel`): an OTLP/HTTP uploader implementing the existing `Uploader` ABC, so ATIF trajectories export to any self-hosted or cloud Langfuse instance.
2. **`packages/harbor-langfuse`**: a `LangfusePlugin(BaseJobPlugin)` mapping Harbor's Job/Dataset/Trial model onto Langfuse's Dataset/Experiment/Trace/Score model, mirroring the existing LangSmith plugin.
3. **Model-path telemetry via LiteLLM proxy** (documented pattern + optional join utility): routing agent model traffic through a LiteLLM proxy whose OpenTelemetry callback emits per-request TTFT/TPOT/token/cost spans, giving latency visibility for **any** agent — including black-box CLI agents — with **zero agent code changes**.

Parts 1–3 are orthogonal: the plugin works without a proxy, the proxy works without the plugin, and together they correlate into one Langfuse project per evaluation deployment.

## 2. Motivation

### 2.1 Self-hosted, OTel-native observability

Langfuse is MIT-licensed and the most widely deployed open-source LLM observability backend. Regulated evaluation environments (government, finance, on-prem labs) frequently require prompts, completions, and scores to remain inside the evaluation network. Harbor already ships a first-party LangSmith plugin and an ATIF→OTel converter with a pluggable uploader; Langfuse is the notable open-source backend with no Harbor equivalent.

The conversion layer already exists: `harbor-atif2otel` emits OpenInference-attributed OTel spans (`openinference.span.kind`, `llm.token_count.*`, `tool.name`, `input.value`/`output.value`), and Langfuse ingests OpenTelemetry natively. What is missing is an uploader (authentication/headers differ) and a management-plane plugin.

### 2.2 Latency truth without agent instrumentation

ATIF trajectories carry step-level timestamps only. Request-level latency signals — time-to-first-token (TTFT), time-per-output-token (TPOT), per-request token usage and cost — exist only inside the model request path and are invisible to the harness.

Two sources can provide them without touching agent code:

- **A model-path proxy** (this RFC): LiteLLM's OpenTelemetry callback emits `gen_ai.server.time_to_first_token` and `gen_ai.server.time_per_output_token` per request. Works for every agent that honors `OPENAI_BASE_URL`/`ANTHROPIC_BASE_URL`-style endpoints — the entire `BaseInstalledAgent` family.
- Agent-side instrumentation (explicitly out of scope here; a possible future RFC could carry request timing in ATIF `step.extra`, which the format already permits).

A proxy is also measurement infrastructure in its own right: production-serving behavior (streaming, cache hit rates, queueing) is only observable from the request path.

## 3. Background: interfaces this RFC builds on

All extension points below already exist on `main`:

| Interface | Location | Contract |
|---|---|---|
| Plugin registration | `[project.entry-points."harbor.plugins"]` | name → `module:Class`; `harbor run --plugin <name>`; `harbor plugins list` |
| Plugin base | `harbor/models/job/plugin.py` | `async on_job_start(job)`, `async on_job_end(job_result)` |
| Trial hooks | `harbor/trial/hooks.py`; `Job.on_trial_started/ended/cancelled(cb)` | `TrialHookEvent(event, task_name, config, result, ...)` |
| ATIF → OTel | `harbor-atif2otel.convert_trajectory()` | ATIF dict → `ResourceSpans` (OpenInference attributes) |
| Uploader ABC | `harbor-atif2otel/uploaders/base.py` | `upload(ResourceSpans)`; only `MlflowProtobufUploader` exists today |
| Verifier rewards | `TrialResult.verifier_result.rewards` | reward name → value mapping; the LangSmith plugin already posts these as feedback |

## 4. Part 1: `LangfuseUploader` (in `packages/harbor-atif2otel`)

```python
class LangfuseUploader(Uploader):
    def __init__(self, host: str, public_key: str, secret_key: str,
                 ingestion_version: str = "4",
                 timeout_seconds: float = 30.0,
                 max_retries: int = 3,
                 throttle_seconds: float = 0.0): ...

    def upload(self, resource_spans: ResourceSpans) -> None: ...
```

Behavior:

- **Endpoint**: `POST {host}/api/public/otel/v1/traces`, OTLP over HTTP, protobuf body (same wire format as the MLflow uploader; stdlib `urllib` only — no new dependencies).
- **Headers**: HTTP Basic auth (`public_key:secret_key`) plus `x-langfuse-ingestion-version: 4`, which enables real-time ingestion on Langfuse v4; without it, directly-ingested OTel data can be delayed by up to 10 minutes.
- **Retry**: 408/429/5xx with exponential backoff (bounded); auth errors fail fast.
- **Selection**: `OtelPlugin._make_uploader()` auto-selects the Langfuse uploader when `LANGFUSE_PUBLIC_KEY` is set; MLflow behavior is unchanged otherwise. `LANGFUSE_HOST` may stand in for `OTEL_EXPORTER_OTLP_ENDPOINT` in `auto` mode.

~150 LOC including tests.

## 5. Part 2: `packages/harbor-langfuse` (management plane)

New workspace package mirroring `packages/harbor-langsmith`:

```
packages/harbor-langfuse/
├── pyproject.toml            # entry point: langfuse = "harbor_langfuse:LangfusePlugin"
└── src/harbor_langfuse/
    ├── plugin.py             # LangfusePlugin(BaseJobPlugin)
    ├── api.py                # thin REST client (Basic auth, retry/backoff)
    ├── enrich.py             # adds langfuse.* filterable attributes to spans
    └── scores.py             # verifier rewards → Scores API
```

### 5.1 `LangfusePlugin` lifecycle

- `on_job_start`: resolve/create a Langfuse **Dataset** (one item per Harbor task; inputs = task name + instruction; items are upserted by stable id, so re-runs are idempotent). Register `job.on_trial_ended(...)`.
- `on_trial_ended`: convert the trial's ATIF trajectory via `harbor-atif2otel` (optional dependency), **enrich** every span with Langfuse-namespaced attributes, upload through `LangfuseUploader`, then post **scores**.
- `on_job_end`: summary log (trials exported, scores written).

### 5.2 Attribute enrichment

Langfuse filters and aggregates on attributes in the `langfuse.*` namespace, which the converter does not emit. Per Langfuse's documented propagation guidance (trace-level attributes must be present on every span, because aggregation operates across observations), `enrich.py` adds to every span:

| Attribute | Source |
|---|---|
| `langfuse.session.id` | `trajectory.session_id` |
| `langfuse.trace.name` | trial name |
| `langfuse.trace.tags` | `["harbor", agent_name, model_name]` |
| `langfuse.experiment.id` / `.name` | Harbor job identity / configured experiment name |
| `langfuse.experiment.dataset.id` | synced dataset id |
| `langfuse.experiment.item.id` | task name |
| `langfuse.user.id` | agent name |

This mirrors Langfuse's documented three-level experiment context (experiment → item → trace) while preserving the one-root-span-per-trial structure.

### 5.3 Scores

Mirroring the LangSmith feedback precedent:

- `TrialResult.verifier_result.rewards` → one score per reward key (`NUMERIC` for numeric values, `CATEGORICAL` for strings) attached to the trial's trace.
- Trials that ended with an exception → a boolean `harbor_error` score, so error rates aggregate in dashboards.
- Scores are upserted by deterministic ids (UUIDv5), making re-exports idempotent.
- **User-defined derived scores**: offline trajectory analysis (computed by the user's own tooling, outside this plugin) may post additional scores through the same public Scores API. The specific metrics are deployment-specific and intentionally out of scope here; the plugin's contract is only that verifier rewards and error state are exported.

### 5.4 Configuration surface

| Option | Environment variable | Default |
|---|---|---|
| `host` | `LANGFUSE_HOST` | `http://localhost:3000` |
| `public_key` / `secret_key` | `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | — |
| `dataset_name` | `HARBOR_LANGFUSE_DATASET` | `harbor-{job_name}` |
| `experiment_name` | `HARBOR_LANGFUSE_EXPERIMENT` | `{job_name}-{job_id[:8]}` |
| `sync_dataset` | `HARBOR_LANGFUSE_SYNC_DATASET` | `true` |
| `export_traces` | `HARBOR_LANGFUSE_EXPORT_TRACES` | `true` |
| `fail_fast` | `HARBOR_LANGFUSE_FAIL_FAST` | `false` |

Usage: `harbor run ... --plugin langfuse` (same ergonomics as `--plugin langsmith`).

## 6. Part 3: Model-path telemetry via LiteLLM proxy

### 6.1 The pattern

```
agent process (unchanged)
   │  model request (streaming), OPENAI_BASE_URL / ANTHROPIC_BASE_URL → proxy
   ▼
LiteLLM proxy  ──OTel callback (OTLP/HTTP)──►  Langfuse
   emits per request:  gen_ai.server.time_to_first_token
                       gen_ai.server.time_per_output_token
                       gen_ai.client.token.usage, gen_ai.usage.cost
```

A minimal proxy config:

```yaml
model_list:
  - model_name: <alias-agents-request>
    litellm_params:
      model: openai/<real-model-name>
      api_base: <backend-inference-endpoint>
litellm_settings:
  callbacks: ["otel"]
```

with `OTEL_EXPORTER_OTLP_ENDPOINT` pointed at Langfuse's OTel endpoint (Basic auth + ingestion-version header, same as Part 1). Routing agents through the proxy is a pure environment-variable change (`OPENAI_BASE_URL`, `OPENAI_API_KEY` — or `ANTHROPIC_BASE_URL` for Anthropic-format agents), well within Harbor's existing env-injection surface.

### 6.2 Trial correlation

Two mechanisms, by agent capability:

1. **Header-based (exact)**: agents built on an SDK may send `x-litellm-session-id` (or arbitrary metadata via `x-litellm-metadata`), which LiteLLM resolves with a fixed priority and forwards onto the OTel span. Proxy traces then land directly in the right Langfuse session — no post-processing.
2. **Time-window join (near-exact)**: for black-box CLI agents, a small join utility queries Langfuse for traces whose timestamps fall inside a trial's `[started_at, finished_at]` window (both available in `TrialResult`) and patches their session id. Under single-concurrency evaluation this is exact; under concurrency it is a bounded time-bucket approximation.

We propose this join as an optional utility — either a standalone script in the plugin package or a documented recipe — and are open to maintainer guidance on where it best lives.

### 6.3 What this buys, and the honest caveats

Per-request TTFT/TPOT/token/cost become visible for any proxied agent, correlated with the trial's trajectory trace under one session. Caveats that belong in any report using this path:

- **Observer effect**: the proxy adds measurable overhead (tens of milliseconds per request in public benchmarks). TTFT measured this way should be labeled "via proxy"; for calibration, an instrumented agent and a proxy can be run simultaneously and compared.
- **TTFT requires streaming**: non-streaming requests report no first-token time.
- **TPOT is an average** (generation time / completion tokens), not a per-token distribution.

## 7. Security and data residency

- All traffic from all three parts targets a user-configured `LANGFUSE_HOST` (typically self-hosted); nothing is sent to any third party.
- Proxy traces carry prompt/completion content; deployments with stricter needs can point the proxy at an internal OTel collector and apply content policy before forwarding, or run the existing `atif2otel` batch mode (`HARBOR_OTEL_OUTPUT_DIR`) fully offline.
- `convert_trajectory()` already supports a `max_attribute_bytes` truncation limit.

## 8. Non-goals

- Agent-side request timing instrumentation (e.g., carrying per-request timing in ATIF `step.extra`). The format permits it; a separate RFC could standardize the field names. This RFC deliberately stops at the harness boundary.
- Live span streaming during agent execution (the current plugin contract is trial-boundary).
- Multimodal media upload to Langfuse's media API (images remain metadata-only, consistent with atif2otel).
- New metrics definitions of any kind: this RFC exports what Harbor already produces (trajectories, rewards) and provides the plumbing for latency signals. Deployment-specific derived metrics are the user's own tooling.

## 9. Alternatives considered

| Alternative | Why not |
|---|---|
| Standalone exporter outside Harbor | Works, but invisible to `harbor plugins list`, no plugin ergonomics, duplicates converter logic |
| Instrument each agent SDK with the Langfuse SDK | Per-agent effort; violates the zero-agent-change property; prompts sent by default |
| Generic OTel Collector sidecar only | Extra ops component; the plugin covers the same path with less setup; a Collector remains compatible for users who already run one |
| Wait for GenAI semantic conventions to stabilize | The uploader emits standard OTLP; attribute semantics remain the converter's concern, not this RFC's |

## 10. Rollout plan

1. **PR-A** — `LangfuseUploader` + auto-detection in `OtelPlugin` + unit tests + README section (~150 LOC).
2. **PR-B** — `packages/harbor-langfuse` plugin package + docs page update + tests (~600 LOC).
3. **PR-C** — model-path telemetry: docs page describing the LiteLLM proxy pattern (config, env injection, correlation recipes, caveats), plus the optional join utility (~200 LOC or docs-only, per maintainer preference).

All parts are default-inert: no behavior change unless Langfuse credentials are provided.

## 11. References

- Langfuse OpenTelemetry ingestion: <https://langfuse.com/integrations/native/opentelemetry>
- Experiments via OpenTelemetry: <https://langfuse.com/integrations/native/opentelemetry/experiments>
- Langfuse Public API: <https://langfuse.com/docs/api-and-data-platform/features/public-api>
- LiteLLM OpenTelemetry metrics (TTFT/TPOT): <https://docs.litellm.ai/docs/observability/opentelemetry_integration>
- LiteLLM proxy request headers (session id resolution): <https://docs.litellm.ai/docs/proxy/request_headers>
- ATIF RFC: `rfcs/0001-trajectory-format.md`

---

## 附录：中文审阅摘要（非上游提交内容）

**定位**：本 RFC 合并此前讨论的两条线——① Langfuse 插件化（原 PR-A 上传器 + PR-B 管理面插件）；② LiteLLM Proxy 模型链路遥测（TTFT/TPOT/token/cost 真值，零 agent 改动）。方案 B（agent 内打点）仅在 Non-goals 提了一句"ATIF `step.extra` 可承载，留待未来 RFC"，未展开。

**已模糊化内容**（自研未公开，全部泛化处理）：
- 具体轨迹质量指标（冗余率/死循环/错误分类等）→ 只写"用户自定义离线派生指标，走同一 Scores API，具体定义 out of scope"
- 三段拆解的特定 score 命名 → 未出现，只在 proxy 部分写了"时间归因于模型/非模型段"的通用描述
- E1–E7 / 现象标签 / 双轨六维等内部体系 → 完全未出现
- demo/原型仓链接 → 未出现（RFC 0001 里有，这版删了，避免暴露内部指标讨论）

**与 RFC 0001 的关系**：本篇取代 0001 的 Langfuse 部分；0001 保留作为接口分析底稿，不重复提交。

**待你拍板**：① PR-C 的 join 工具放插件包内还是独立脚本（RFC 里写了两可，倾向问 maintainer）；② 代理观察者效应的表述力度（当前写法把 +40ms 泛化为"tens of milliseconds"，未引具体基准）；③ 标题是否要点出"black-box agents"卖点。
