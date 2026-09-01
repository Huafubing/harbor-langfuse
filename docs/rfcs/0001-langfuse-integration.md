# RFC 0001: Langfuse Integration for Harbor

- **Status**: Draft
- **Author**: Libotry ([github.com/Libotry](https://github.com/Libotry))
- **Target repository**: `harbor-framework/harbor`
- **Related**: RFC 0001 (ATIF), `packages/harbor-atif2otel`, `packages/harbor-langsmith`
- **Prototype**: [github.com/Libotry/harbor-langfuse](https://github.com/Libotry/harbor-langfuse) (standalone exporter, validated end-to-end)

---

## 1. Summary

This RFC proposes two small, composable PRs that give Harbor first-class [Langfuse](https://langfuse.com) support, mirroring the architecture of the existing LangSmith integration:

- **PR-A — `LangfuseUploader`** in `packages/harbor-atif2otel`: an OTLP/HTTP uploader implementing the existing `Uploader` ABC, so every ATIF trajectory can be exported to any self-hosted or cloud Langfuse instance.
- **PR-B — `packages/harbor-langfuse`**: a `LangfusePlugin(BaseJobPlugin)` that maps Harbor's Job/Dataset/Trial model onto Langfuse's Dataset/Experiment/Trace/Score model, mirroring `LangSmithPlugin`.

Both are zero-agent-intrusion: they read only Harbor's own artifacts (`trajectory.json`, `TrialResult`) and hook only Harbor's public plugin API. No third-party agent (claude-code, openhands, codex, ...) requires any modification.

## 2. Motivation

1. **Data residency.** Langfuse is MIT-licensed and deploys self-hosted via docker-compose. Regulated deployments (government, finance, on-prem evaluation labs) frequently require that prompts, completions and scores never leave the evaluation network. A plugin that targets a user-provided `LANGFUSE_HOST` makes Harbor usable in those environments without any code change.
2. **Parity with LangSmith.** Harbor already ships a first-party LangSmith plugin (dataset sync + experiment sessions + verifier feedback). Langfuse is the most widely deployed open-source LLM observability backend and has no Harbor equivalent today.
3. **The hard part already exists.** `harbor-atif2otel` already converts ATIF trajectories into OpenInference-attributed OTel spans with a pluggable uploader. Langfuse ingests OpenInference attributes natively over OTLP. What is missing is a ~100-line uploader and a management-plane plugin.
4. **Validated prototype.** A standalone exporter (`atif2langfuse`, see prototype repo) has been built and tested against Langfuse v4 self-hosted: trace tree (root/LLM/TOOL), scores, and experiment grouping all verified. This RFC upstreams that design.

## 3. Background: interfaces this RFC builds on

All extension points referenced below already exist on `main`:

| Interface | Location | Contract |
|---|---|---|
| Plugin registration | `pyproject.toml` → `[project.entry-points."harbor.plugins"]` | name → `module:Class`; resolved by `harbor/cli/plugin_registry.py`; `harbor run --plugin <name\|module:Class>` |
| Plugin base | `harbor/models/job/plugin.py` → `BaseJobPlugin` | `async on_job_start(job)`, `async on_job_end(job_result)` |
| Trial hooks | `harbor/trial/hooks.py` → `TrialEvent`, `TrialHookEvent`; `Job.on_trial_started/ended/cancelled(cb)` | fired with `TrialHookEvent(event, task_name, config, result, lock, trial_id)` |
| ATIF → OTel | `packages/harbor-atif2otel` → `convert_trajectory()` | ATIF dict → `ResourceSpans` (OpenInference: `openinference.span.kind`=AGENT/LLM/TOOL, `session.id`, `llm.token_count.*`, `llm.cost.total`, `tool.name`, `input.value`, `output.value`) |
| Uploader ABC | `packages/harbor-atif2otel/.../uploaders/base.py` | `upload(ResourceSpans)`; `upload_batch()` default impl; only `MlflowProtobufUploader` exists today |
| Verifier rewards | `TrialResult.verifier_result.rewards` | dict of reward name → value; consumed by `LangSmithPlugin._create_feedback()` precedent |

## 4. PR-A: `LangfuseUploader` (in `packages/harbor-atif2otel`)

### 4.1 Interface

```python
# packages/harbor-atif2otel/src/harbor_atif2otel/uploaders/langfuse.py

class LangfuseUploader(Uploader):
    """Upload OTel ResourceSpans to Langfuse via OTLP/HTTP.

    Args:
        host: Langfuse base URL, e.g. "https://langfuse.example.com"
              (env: LANGFUSE_HOST)
        public_key: Langfuse public key  "pk-lf-..."  (env: LANGFUSE_PUBLIC_KEY)
        secret_key: Langfuse secret key  "sk-lf-..."  (env: LANGFUSE_SECRET_KEY)
        ingestion_version: Langfuse v4 real-time ingestion header (default "4")
        timeout_seconds: per-request timeout (default 15)
        max_retries: retries on 408/429/5xx with exponential backoff (default 3)
    """

    def __init__(self, host: str, public_key: str, secret_key: str, ...) -> None: ...

    def upload(self, resource_spans: ResourceSpans) -> None: ...
```

### 4.2 Behavior

- **Endpoint**: `POST {host}/api/public/otel/v1/traces`, OTLP over HTTP. Body: `ExportTraceServiceRequest` protobuf (same as MLflow uploader) — Langfuse accepts both protobuf and JSON; protobuf is chosen for dependency parity.
- **Headers**:
  - `Authorization: Basic base64("{public_key}:{secret_key}")`
  - `x-langfuse-ingestion-version: 4` — required for real-time ingestion on Langfuse v4; without it, directly-ingested OTel data may be delayed by up to 10 minutes.
- **Retry**: on `408/429/5xx` with exponential backoff (mirrors `LangSmithPlugin`'s retryable status set). Raise `RuntimeError` after `max_retries` (caller — `OtelPlugin` — already logs and continues per-trial).
- **Throttling**: optional `throttle_seconds` like MLflow uploader (default 0 for self-hosted).
- **No new dependencies**: stdlib `urllib.request` + `opentelemetry-proto`, both already package dependencies.

### 4.3 Wiring into `OtelPlugin`

`OtelPlugin._make_uploader()` gains auto-detection:

```python
if os.getenv("LANGFUSE_PUBLIC_KEY"):
    return LangfuseUploader(host=..., public_key=..., secret_key=...)
if self._experiment_name or os.getenv("MLFLOW_TRACKING_TOKEN"):
    return MlflowProtobufUploader(...)   # existing behavior unchanged
```

MLflow behavior is fully preserved; Langfuse is selected only when Langfuse credentials are present.

## 5. PR-B: `packages/harbor-langfuse` (management plane)

New workspace package, structured after `packages/harbor-langsmith`:

```
packages/harbor-langfuse/
├── pyproject.toml            # [project.entry-points."harbor.plugins"] langfuse = "harbor_langfuse.plugin:LangfusePlugin"
├── README.md
└── src/harbor_langfuse/
    ├── __init__.py
    ├── plugin.py             # LangfusePlugin(BaseJobPlugin)
    ├── enrich.py             # ResourceSpans attribute enrichment (experiment/session/tags)
    └── scores.py             # verifier rewards → Langfuse Scores API
```

### 5.1 `LangfusePlugin`

```python
class LangfusePlugin(BaseJobPlugin):
    def __init__(self, *,
                 host: str | None = None,            # env LANGFUSE_HOST
                 public_key: str | None = None,      # env LANGFUSE_PUBLIC_KEY
                 secret_key: str | None = None,      # env LANGFUSE_SECRET_KEY
                 dataset_name: str | None = None,    # env HARBOR_LANGFUSE_DATASET
                 experiment_name: str | None = None, # env HARBOR_LANGFUSE_EXPERIMENT
                 release: str | None = None,
                 sync_dataset: bool = True,
                 export_traces: bool = True,         # reuse atif2otel converter internally
                 fail_fast: bool = False):
```

Lifecycle:

1. `on_job_start`: resolve/create Langfuse **Dataset** (`POST /api/public/datasets`, items = task instructions via `POST /api/public/dataset-items`), resolve/create **Experiment** identity. Register `job.on_trial_ended(...)`.
2. `on_trial_ended(event)`:
   - `convert_trajectory()` (from `harbor-atif2otel`, optional dependency) → `ResourceSpans`;
   - `enrich()` adds Langfuse filterable attributes to every span (see §5.2);
   - `LangfuseUploader.upload(...)` when `export_traces=True`;
   - `scores.backfill()` — mirror of `LangSmithPlugin._create_feedback`: `TrialResult.verifier_result.rewards` → `POST /api/public/score` with `{traceId, observationId, name: key (or "reward.<key>"), value, dataType: "NUMERIC"}`; trial exceptions produce a boolean `harbor_error` score. Stable UUIDs prevent duplicates on retry.
3. `on_job_end`: summary log (converted / errors / scores written).

### 5.2 Attribute enrichment (the only mapping gap)

Langfuse's OTel ingestion natively understands OpenInference attributes (span kind, token counts, input/output). What it additionally filters on are Langfuse-namespaced attributes, which `convert_trajectory()` does not emit. `enrich.py` adds, to **every** span (Langfuse aggregates across observations, not only root spans):

| Added attribute | Source |
|---|---|
| `langfuse.session.id` | `trajectory.session_id` |
| `langfuse.trace.name` | `"{task}#{attempt}"` |
| `langfuse.trace.tags` | `["harbor", agent_name, model_name]` |
| `langfuse.experiment.id` / `.name` | Harbor `job_id` / configured experiment name |
| `langfuse.experiment.dataset.id` | synced dataset id (when `sync_dataset=True`) |
| `langfuse.experiment.item.id` | task name |
| `langfuse.user.id` | agent name |

This mirrors Langfuse's documented "experiments via OTel" three-level context model (experiment baggage → item root → item context) while keeping the single-root-span-per-item requirement.

### 5.3 Configuration surface

| Env var | Plugin kwarg | Default |
|---|---|---|
| `LANGFUSE_HOST` | `host` | `http://localhost:3000` |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | `public_key` / `secret_key` | — |
| `HARBOR_LANGFUSE_DATASET` | `dataset_name` | — |
| `HARBOR_LANGFUSE_EXPERIMENT` | `experiment_name` | job name |
| `HARBOR_LANGFUSE_SYNC_DATASET` | `sync_dataset` | `true` |
| `HARBOR_LANGFUSE_EXPORT_TRACES` | `export_traces` | `true` |
| `HARBOR_LANGFUSE_FAIL_FAST` | `fail_fast` | `false` |

Usage: `harbor run ... --plugin langfuse`, or `--plugin harbor_langfuse:LangfusePlugin`, or job-config `plugins:` with `kwargs:` (identical ergonomics to `--plugin langsmith`).

## 6. Security and data residency

- All network traffic targets the user-configured `LANGFUSE_HOST`; nothing is sent to any third party. Self-hosted deployments keep prompts/completions/scores inside the evaluation network.
- ATIF `input.value`/`output.value` carry message content. Deployments with stricter requirements can run the existing `OtelPlugin` batch mode (`HARBOR_OTEL_OUTPUT_DIR`) offline and apply content policy before upload; a `max_attribute_bytes` truncation limit already exists in `convert_trajectory()`. A `HARBOR_LANGFUSE_REDACT=true` mode (hash-and-length substitution for string leaves) exists in the prototype and can be ported in a follow-up.

## 7. Non-goals / future work

- **Step-internal timing (TTFT/TPOT)**: ATIF trajectories carry step-level timestamps only; real LLM-request timing must come from a model-path event source (native agent telemetry, a model proxy, or an environment wrapper in the style of `src/harbor/environments/langsmith.py`). Candidate follow-up RFC; explicitly out of scope here.
- Live span streaming during agent execution (current plugin contract is trial-boundary).
- Multimodal media upload to Langfuse's media API (images currently metadata-only, consistent with atif2otel).

## 8. Alternatives considered

| Alternative | Why not |
|---|---|
| Standalone exporter outside Harbor (prototype: `atif2langfuse`) | Works, but invisible to `harbor plugins list`, no plugin ergonomics, no dataset/experiment management; duplicates convert logic already in atif2otel |
| Instrument agent SDKs with Langfuse SDK | Violates zero-agent-intrusion; per-agent effort; leaks prompts by default |
| Generic OTEL Collector sidecar | Extra ops component for users; plugin covers the same path with less setup; Collector remains an option for users who already run one (OtelPlugin endpoint works with it) |
| Langfuse-native environment wrapper (like `environments/langsmith.py`) | Higher value but much larger surface; better as follow-up once plugin lands |

## 9. Rollout plan

1. **PR-A** (`packages/harbor-atif2otel`): `uploaders/langfuse.py` + auto-detect in `_make_uploader` + unit tests (mock HTTP via `urllib` patch) + README section. ~150 LOC.
2. **PR-B** (`packages/harbor-langfuse`): plugin package + `docs-mintlify/core-concepts/plugins/existing-plugins.mdx` update + docs page + golden tests with recorded Langfuse responses. ~600 LOC.
3. Both default-inert: no behavior change unless Langfuse credentials/config are provided.

## 10. References

- Langfuse OTel ingestion: <https://langfuse.com/integrations/native/opentelemetry>
- Experiments via OTel: <https://langfuse.com/integrations/native/opentelemetry/experiments>
- Scores API: <https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk>
- ATIF RFC: `rfcs/0001-trajectory-format.md`
- Prototype (validated): <https://github.com/Libotry/harbor-langfuse>
- Interface analysis of this RFC (Chinese, internal): `docs/upstream-interface-analysis.md`

---

## 附录：中文摘要（供内部评审，非上游提交内容）

**提案**：向 harbor-framework 上游提两个 PR。PR-A 在 `harbor-atif2otel` 里加 `LangfuseUploader`（约 150 行：OTLP/HTTP + Basic 认证 + v4 实时入库头 + 重试），使现有 OtelPlugin 自动支持 Langfuse；PR-B 新增 `packages/harbor-langfuse`（对齐官方 langsmith 插件形态：Dataset 同步 + Experiment 映射 + verifier 判分回填 Scores + 属性富集），约 600 行。

**关键结论**：无需发明任何新协议——插件注册（entry points）、插件基类（BaseJobPlugin）、Trial 钩子、ATIF→OTel 转换（OpenInference 语义）、Uploader 抽象、判分回填先例（`verifier_result.rewards` → feedback）全部已在上游存在；Langfuse 原生吃 OpenInference 属性，缺的只是认证头不同的上传器和管理面插件。**三方 Agent 业务代码零改动**；TTFT/TPOT 时延真值需另行提供模型链路事件源（参考 `environments/langsmith.py` 的环境包装模式），列为后续 RFC。

**与本地原型关系**：本仓 `src/atif2langfuse/`（独立导出器）为已验证原型，接口设计已被本 RFC 吸收；上游合并后原型保留作为离线补导工具。
