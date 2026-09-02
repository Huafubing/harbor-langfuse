# RFC 0003：Harbor 作业的 Langfuse 可观测性 — 插件集成与面向黑盒 Agent 的模型链路遥测

> 本文档是 [0003-langfuse-observability.md](0003-langfuse-observability.md) 的中文翻译版，供内部评审使用。英文版为提交上游的正式文本；若两版含义有出入，以英文版为准。内部审阅备注见 [0003-langfuse-observability.internal-notes.zh.md](0003-langfuse-observability.internal-notes.zh.md)。
>
> 状态：草稿（尚未提交上游） · 作者：Libotry · 目标仓库：`harbor-framework/harbor` · 关联：RFC 0001（ATIF）、`packages/harbor-atif2otel`、`packages/harbor-langsmith` · 核对基线：`harbor-framework/harbor` main @ `6af8d6e`

---

## 1. 概述

本 RFC 提议为 Harbor 提供第一方的 [Langfuse](https://langfuse.com) 可观测能力，由三个可组合的部分构成：

1. **`LangfuseUploader`**（扩展 `packages/harbor-atif2otel`）：实现既有 `Uploader` 抽象基类的 OTLP/HTTP 上传器，使 ATIF 轨迹可导出到任意自托管或云端 Langfuse 实例。
2. **`packages/harbor-langfuse`**：一个 `LangfusePlugin(BaseJobPlugin)`，把 Harbor 的 Job/Dataset/Trial 模型映射到 Langfuse 的 Dataset/Experiment/Trace/Score 模型，与现有 LangSmith 插件对齐。
3. **经 LiteLLM proxy 的模型链路遥测**（文档化模式 + 可选 join 工具）：将 agent 的模型流量路由到 LiteLLM proxy，其 OpenTelemetry callback 逐请求上报 TTFT/TPOT/token/cost span，为**任意** agent（包括黑盒 CLI agent）提供时延可见性，且**零 agent 代码改动**。

第 1–3 部分相互正交：插件可脱离 proxy 工作，proxy 可脱离插件工作，组合起来则在同一评测部署内关联到同一个 Langfuse 项目。

## 2. 动机

### 2.1 自托管、OTel 原生的可观测性

Langfuse 采用 MIT 许可，是部署最广泛的开源 LLM 可观测后端之一。受监管的评测环境（政务、金融、本地机房）通常要求 prompt、补全内容与判分留在评测网络之内。Harbor 已内置第一方的 LangSmith 插件和带可插拔上传器的 ATIF→OTel 转换器；Langfuse 是其中明显缺失对应的开源后端。

转换层其实已经存在：`harbor-atif2otel` 产出带 OpenInference 属性的 OTel span（`openinference.span.kind`、`llm.token_count.*`、`tool.name`、`input.value`/`output.value`），而 Langfuse 原生摄取 OpenTelemetry。缺的只是一个上传器（认证与请求头不同）和一个管理面插件。

### 2.2 无需 agent 埋点的时延真值

ATIF 轨迹只携带 step 级时间戳。请求级时延信号——首 token 时延（TTFT）、逐 token 时延（TPOT）、单请求 token 用量与成本——只存在于模型请求路径内部，对 harness 不可见。

有两个不改 agent 代码即可获取它们的事件源：

- **模型链路 proxy**（本 RFC）：LiteLLM 的 OpenTelemetry callback 逐请求上报 `gen_ai.server.time_to_first_token` 与 `gen_ai.server.time_per_output_token`。适用于一切遵循 `OPENAI_BASE_URL`/`ANTHROPIC_BASE_URL` 类端点的 agent——即整个 `BaseInstalledAgent` 家族。
- Agent 侧埋点（本文明确列为范围外；未来的 RFC 可将请求级计时写入 ATIF `step.extra`，格式本身已允许）。

Proxy 本身也是独立的测量基础设施：生产化 Serving 行为（流式、缓存命中、排队）只有从请求路径上才可观测。

## 3. 背景：本 RFC 依托的既有接口

以下扩展点均已存在于 `main` 分支：

| 接口 | 位置 | 契约 |
|---|---|---|
| 插件注册 | `[project.entry-points."harbor.plugins"]` | 名称 → `module:Class`；`harbor run --plugin <name>`；`harbor plugins list` |
| 插件基类 | `harbor/models/job/plugin.py` | `async on_job_start(job)`、`async on_job_end(job_result)` |
| Trial 钩子事件 | `harbor/trial/hooks.py` | `TrialHookEvent(event, task_name, config, result, ...)` |
| Trial 钩子注册 | `harbor/job.py` 中的 `Job.on_trial_started/ended/cancelled(cb)` | 按 job 注册异步回调 |
| ATIF → OTel | `harbor-atif2otel.convert_trajectory()` | ATIF dict → `ResourceSpans`（OpenInference 属性） |
| Uploader 抽象 | `harbor-atif2otel/uploaders/base.py` | `upload(ResourceSpans)`；目前仅有 `MlflowProtobufUploader` |
| Verifier 判分 | `TrialResult.verifier_result.rewards` | 判分名 → 值的映射；LangSmith 插件已将其作为 feedback 上报 |

## 4. 第一部分：`LangfuseUploader`（位于 `packages/harbor-atif2otel`）

```python
class LangfuseUploader(Uploader):
    def __init__(self, host: str, public_key: str, secret_key: str,
                 ingestion_version: str = "4",
                 timeout_seconds: float = 30.0,
                 max_retries: int = 3,
                 throttle_seconds: float = 0.0): ...

    def upload(self, resource_spans: ResourceSpans) -> None: ...
```

行为：

- **端点**：`POST {host}/api/public/otel/v1/traces`，OTLP over HTTP，protobuf 请求体（与 MLflow 上传器同一线上格式；传输层仅用标准库 `urllib`，除 `harbor-atif2otel` 既有的 `opentelemetry-proto` 外不新增依赖）。
- **请求头**：HTTP Basic 认证（`public_key:secret_key`）外加 `x-langfuse-ingestion-version: 4`，后者开启 Langfuse v4 的实时摄取；缺省该头时，直连摄取的 OTel 数据可能延迟最多 10 分钟。
- **重试**：408/429/5xx 指数退避（有上限）；认证类错误立即失败。
- **选择逻辑（本 RFC 提议的改动）**：`OtelPlugin._make_uploader()` 目前无条件返回 `MlflowProtobufUploader`；本 RFC 将其扩展为：设置 `LANGFUSE_PUBLIC_KEY` 时自动选用 Langfuse 上传器，否则 MLflow 行为不变。`auto` 模式下 `LANGFUSE_HOST` 可替代 `OTEL_EXPORTER_OTLP_ENDPOINT`。

含测试约 150 行。

## 5. 第二部分：`packages/harbor-langfuse`（管理面）

新 workspace 包，形态对齐 `packages/harbor-langsmith`：

```
packages/harbor-langfuse/
├── pyproject.toml            # entry point: langfuse = "harbor_langfuse:LangfusePlugin"
└── src/harbor_langfuse/
    ├── plugin.py             # LangfusePlugin(BaseJobPlugin)
    ├── api.py                # 轻量 REST 客户端（Basic 认证、重试/退避）
    ├── enrich.py             # 为 span 增补 langfuse.* 可过滤属性
    └── scores.py             # verifier 判分 → Scores API
```

### 5.1 `LangfusePlugin` 生命周期

- `on_job_start`：解析/创建 Langfuse **Dataset**（每个 Harbor 任务一个 item；输入 = 任务名 + 指令；item 按稳定 id upsert，重跑幂等）。注册 `job.on_trial_ended(...)`。
- `on_trial_ended`：经 `harbor-atif2otel`（可选依赖）转换该 trial 的 ATIF 轨迹，为全部 span **富集** Langfuse 命名空间属性，经 `LangfuseUploader` 上传，随后回填 **scores**。
- `on_job_end`：输出汇总日志（导出 trial 数、写入 scores 数）。

### 5.2 属性富集

Langfuse 按 `langfuse.*` 命名空间的属性做过滤与聚合，而转换器不产出这些属性。按 Langfuse 官方的传播指引（trace 级属性必须存在于每个 span 上，因为聚合作用于所有 observation），`enrich.py` 为每个 span 增补：

| 属性 | 来源 |
|---|---|
| `langfuse.session.id` | `trajectory.session_id` |
| `langfuse.trace.name` | trial 名 |
| `langfuse.trace.tags` | `["harbor", agent 名, 模型名]` |
| `langfuse.experiment.id` / `.name` | Harbor job 标识 / 配置的实验名 |
| `langfuse.experiment.dataset.id` | 已同步的 dataset id |
| `langfuse.experiment.item.id` | 任务名 |
| `langfuse.user.id` | agent 名 |

这对应 Langfuse 文档化的三层实验上下文（experiment → item → trace），同时保持"每 trial 一个根 span"的结构。

### 5.3 Scores

对齐 LangSmith 的 feedback 先例：

- `TrialResult.verifier_result.rewards` → 每个判分键一条 `NUMERIC` score，挂在该 trial 的 trace 上（上游当前 rewards 类型为 `dict[str, float | int]`；若未来判分类型扩展，其余 score 数据类型仍可启用）。
- 以异常收尾的 trial → 布尔型 `harbor_error` score，使错误率可在看板聚合。
- Scores 按确定性 id（UUIDv5）upsert，重导幂等。
- **用户自定义派生指标**：离线轨迹分析（由用户自己的工具计算，在本插件之外）可通过同一公开 Scores API 回填更多 scores。具体指标因部署而异，本文刻意不纳入范围；插件的契约仅是导出 verifier 判分与错误状态。

### 5.4 配置面

| 选项 | 环境变量 | 默认值 |
|---|---|---|
| `host` | `LANGFUSE_HOST` | `http://localhost:3000` |
| `public_key` / `secret_key` | `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | — |
| `dataset_name` | `HARBOR_LANGFUSE_DATASET` | `harbor-{job_name}` |
| `experiment_name` | `HARBOR_LANGFUSE_EXPERIMENT` | `{job_name}-{job_id[:8]}` |
| `sync_dataset` | `HARBOR_LANGFUSE_SYNC_DATASET` | `true` |
| `export_traces` | `HARBOR_LANGFUSE_EXPORT_TRACES` | `true` |
| `fail_fast` | `HARBOR_LANGFUSE_FAIL_FAST` | `false` |

用法：`harbor run ... --plugin langfuse`（与 `--plugin langsmith` 同一套人机接口）。

## 6. 第三部分：经 LiteLLM proxy 的模型链路遥测

### 6.1 模式

```
agent 进程（不改动）
   │  模型请求（流式），OPENAI_BASE_URL / ANTHROPIC_BASE_URL → proxy
   ▼
LiteLLM proxy ──OTel callback（OTLP/HTTP）──► Langfuse
   逐请求上报：gen_ai.server.time_to_first_token
               gen_ai.server.time_per_output_token
               gen_ai.client.token.usage、gen_ai.usage.cost
```

最小 proxy 配置：

```yaml
model_list:
  - model_name: <agent 请求的别名>
    litellm_params:
      model: openai/<真实模型名>
      api_base: <后端推理端点>
litellm_settings:
  callbacks: ["otel"]
```

`OTEL_EXPORTER_OTLP_ENDPOINT` 指向 Langfuse 的 OTel 端点（Basic 认证 + ingestion-version 头，与第一部分相同）。将 agent 路由到 proxy 是纯粹的环境变量改动（`OPENAI_BASE_URL`、`OPENAI_API_KEY`——Anthropic 格式 agent 用 `ANTHROPIC_BASE_URL`）；无论部署侧如何配置 agent 环境，都不需要改动 Harbor 或 agent 代码。

### 6.2 Trial 关联

按 agent 能力分两级：

1. **基于请求头（精确）**：基于 SDK 构建的 agent 可发送 session 请求头，LiteLLM 按固定优先级解析——先 `x-litellm-trace-id`，其次 `x-litellm-session-id`，再次任意 `x-<vendor>-session-id` 头（如 `x-claude-code-session-id` 会被自动识别）。最后这一模式意味着：已自带 session 头的 agent 可以免费获得关联。Proxy trace 因此直接落入正确的 Langfuse session——无需后处理。（实现备注：session id 是否传播到 OTel span 属性，需在 PR-C 落地前对当前 LiteLLM 版本实测确认。）
2. **时间窗 join（近精确）**：对黑盒 CLI agent，一个小型 join 工具查询 Langfuse 中时间戳落在某 trial `[started_at, finished_at]` 窗口（两者都来自 `TrialResult`）内的 trace，并 PATCH 其 session id。单并发评测下这是精确的；并发下是有界的时间分桶近似。

我们建议将此 join 做成可选工具——插件包内的独立脚本或文档化 recipe 皆可——具体落点欢迎 maintainer 给意见。

### 6.3 收益与诚实的局限

对任何走 proxy 的 agent，逐请求 TTFT/TPOT/token/cost 变为可见，并在同一 session 下与该 trial 的轨迹 trace 关联。使用该路径的报告必须写明以下口径：

- **观察者效应**：proxy 会引入可测量的开销（公开基准中为每请求数十毫秒量级）。如此测得的 TTFT 应标注"经 proxy 口径"；若推理后端自身暴露请求级遥测，可用两者对比来校准 proxy 开销。
- **TTFT 依赖流式**：非流式请求没有首 token 时刻。
- **TPOT 是均值**（生成时长 / 补全 token 数），不是逐 token 分布。

## 7. 安全与数据驻留

- 三部分的全部流量都指向用户配置的 `LANGFUSE_HOST`（通常为自托管）；不向任何第三方发送数据。
- Proxy trace 携带 prompt/补全内容；要求更严的部署可将 proxy 指向内部 OTel collector，先做内容策略再转发；或完全离线使用既有 `atif2otel` 批处理模式（`HARBOR_OTEL_OUTPUT_DIR`）。
- `convert_trajectory()` 已支持 `max_attribute_bytes` 截断上限。

## 8. 范围外（Non-goals）

- Agent 侧请求级计时埋点（如把单请求计时写入 ATIF `step.extra`）。格式已允许；字段名的标准化可另立 RFC。本 RFC 刻意止步于 harness 边界。
- agent 执行期间的实时 span 流式（当前插件契约为 trial 边界）。
- 向 Langfuse 媒体 API 上传多模态内容（与 atif2otel 一致，图片仅作为元数据）。
- 不新增任何指标定义：本 RFC 导出的是 Harbor 已有产物（轨迹、判分）并为时延信号提供管道。部署特定的派生指标属于用户自己的工具。

## 9. 已考虑的替代方案

| 替代方案 | 不采用原因 |
|---|---|
| Harbor 之外的独立导出器 | 可用，但对 `harbor plugins list` 不可见、无插件使用体验、重复实现转换逻辑 |
| 为每个 agent SDK 埋 Langfuse SDK | 逐 agent 适配成本；破坏零 agent 改动属性；默认外发 prompt |
| 仅用通用 OTel Collector sidecar | 额外运维组件；插件以更少装配覆盖同一路径；已有 Collector 的用户仍可兼容使用 |
| 等待 GenAI 语义约定稳定 | 上传器输出的是标准 OTLP；属性语义归转换器管，不在本 RFC 范围 |

## 10. 落地计划

1. **PR-A** — `LangfuseUploader` + `OtelPlugin` 自动探测 + 单测 + README 章节（约 150 行）。
2. **PR-B** — `packages/harbor-langfuse` 插件包 + 文档页更新 + 测试（约 600 行）。
3. **PR-C** — 模型链路遥测：描述 LiteLLM proxy 模式的文档页（配置、env 注入、关联 recipe、口径说明）+ 可选 join 工具（约 200 行或仅文档，由 maintainer 定）。

各部分默认惰性：未提供 Langfuse 凭证时无任何行为变化。

## 11. 参考

- Langfuse OpenTelemetry 摄取：<https://langfuse.com/integrations/native/opentelemetry>
- Langfuse v4 摄取迁移（`x-langfuse-ingestion-version`）：<https://langfuse.com/integrations/native/opentelemetry/migration-to-v4>
- 经 OpenTelemetry 的实验：<https://langfuse.com/integrations/native/opentelemetry/experiments>
- Langfuse Public API：<https://langfuse.com/docs/api-and-data-platform/features/public-api>
- LiteLLM OpenTelemetry 指标（TTFT/TPOT）：<https://docs.litellm.ai/docs/observability/opentelemetry_integration>
- LiteLLM proxy 请求头（session id 解析）：<https://docs.litellm.ai/docs/proxy/request_headers>
- ATIF RFC：`rfcs/0001-trajectory-format.md`
