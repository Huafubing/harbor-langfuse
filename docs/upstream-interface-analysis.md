# 上游 harbor-framework 接口形态分析（2026-09-01，main@c0acdfbf）

> 本文档是 RFC 0001（`docs/rfcs/0001-langfuse-integration.md`）的调研底稿。源码快照：`/root/harbor-upstream`。

## 1. 总览

上游 main 分支已包含完整的轨迹观测扩展体系，Langfuse 集成缺的只是"最后一个上传器 + 一个管理面插件"：

| 组件 | 状态 | 说明 |
|---|---|---|
| `harbor.plugins` entry points | ✅ 已存在 | `harbor/cli/plugin_registry.py`，`--plugin <name\|module:Class>`，`harbor plugins list` |
| `BaseJobPlugin` | ✅ 已存在 | `on_job_start(job)` / `on_job_end(job_result)` 两个抽象方法 |
| Trial 钩子 | ✅ 已存在 | `trial/hooks.py`：`TrialEvent`（START/ENVIRONMENT_START/AGENT_START/AGENT_END/VERIFICATION_START/END/CANCEL）+ `TrialHookEvent`，`job.on_trial_started/ended/cancelled(cb)` |
| ATIF→OTel 转换 | ✅ 已存在 | `packages/harbor-atif2otel`：OpenInference 属性，`convert_trajectory()` |
| Uploader 抽象 | ✅ 已存在 | `uploaders/base.py`：`upload(ResourceSpans)`；唯一实现是 MLflow |
| LangSmith 插件 | ✅ 第一方 | dataset 同步 + experiment session + `verifier_result.rewards`→`/feedback` |
| LangSmith 环境包装 | ✅ 第一方 | `src/harbor/environments/langsmith.py`（1541 行，`EnvironmentType.LANGSMITH`，注入 `LANGSMITH_*`） |
| **Langfuse 上传器** | ❌ 缺 | PR-A 目标 |
| **Langfuse 管理面插件** | ❌ 缺 | PR-B 目标 |
| 步骤内时延（TTFT/TPOT） | ❌ 不存在 | ATIF 无此数据，需模型链路事件源（后续 RFC） |

注意：`src/harbor/telemetry.py` 是 PostHog 产品统计（job_finished 用量事件），与轨迹观测无关，勿混淆。

## 2. 关键接口签名

### 插件注册（pyproject.toml）

```toml
[project.entry-points."harbor.plugins"]
atif2otel = "harbor_atif2otel.plugin:OtelPlugin"
```

### BaseJobPlugin / 钩子

```python
class BaseJobPlugin(ABC):
    @abstractmethod
    async def on_job_start(self, job: Job) -> None: ...
    @abstractmethod
    async def on_job_end(self, job_result: JobResult) -> None: ...

# 动态注册逐 trial 回调
job.on_trial_started(cb); job.on_trial_ended(cb); job.on_trial_cancelled(cb)
# TrialHookEvent: event/task_name/config/result/lock/trial_id(计算属性)
```

### Uploader ABC（atif2otel）

```python
class Uploader(ABC):
    @abstractmethod
    def upload(self, resource_spans: ResourceSpans) -> None: ...
    def upload_batch(self, batch) -> tuple[int, int]: ...
```

MLflow 实现要点：标准库 `urllib.request`（零新增依赖）、`ExportTraceServiceRequest.SerializeToString()`、POST `{endpoint}/v1/traces`、`content-type: application/x-protobuf`、503 重试一次、0.5s 节流。

### OtelPlugin 行为

- `__init__(endpoint, output_dir, experiment_name, token, workspace, encoding, mode)`；env 兜底 `OTEL_EXPORTER_OTLP_ENDPOINT` / `HARBOR_OTEL_OUTPUT_DIR` / `MLFLOW_EXPERIMENT_NAME`
- `mode=auto|stream|batch`：stream=每 trial 结束即上传（`job.on_trial_ended`）；batch=job 结束统一转换落盘
- 转换入口：`export_trial(trial_dir, uploader=...)` → 内部读 `agent/trajectory.json`

### ATIF→OTel 映射（OpenInference 语义）

| ATIF | OTel span |
|---|---|
| Trajectory | 根 AGENT span |
| 对话轮次（多轮） | 嵌套 AGENT span |
| agent Step | LLM span |
| tool_calls[] | TOOL span（LLM 的兄弟节点） |
| subagent 委派 | 嵌套 AGENT 子树 |

属性：`openinference.span.kind`、`session.id`、`llm.model_name`、`llm.token_count.prompt/completion`、`llm.token_count.prompt_details.cache_read`、`llm.cost.total`、`tool.name`、`input.value`、`output.value`。v1.7 特性（subagent/embedding/multimodal 文本抽取/`is_copied_context` 过滤）全部支持。

### LangSmithPlugin 判分回填先例（PR-B 的模板）

```python
# plugin.py: _create_feedback(run_id, result)
for key, score in result.verifier_result.rewards.items():
    POST {base}/feedback {"id": stable_uuid(run_id, "feedback", key), "score": score, ...}
# 异常 trial：boolean "harbor_error" feedback，score=1/0
```

Langfuse 对应物：`POST /api/public/score`，`{traceId, observationId, name: key|reward.<key>, value, dataType: NUMERIC}`，稳定 UUID 防重复。

### LangSmith 环境包装（未来时延增强的模式参考）

`environments/langsmith.py`：注册 `EnvironmentType.LANGSMITH`，容器内注入 `LANGSMITH_*`（含 `LANGSMITH_SANDBOX_API_URL`），agent 自动上报——即"档1 注入"的官方先例。

## 3. Langfuse 侧映射缺口（即 PR 内容）

1. **认证头**：Langfuse 用 Basic(pk:sk) + `x-langfuse-ingestion-version: 4`（缺省延迟可达 10 分钟），endpoint `{host}/api/public/otel/v1/traces`，仅 OTLP/HTTP（无 gRPC）
2. **过滤属性**：Langfuse 按 `langfuse.session.id / langfuse.trace.tags / langfuse.experiment.*` 聚合筛选，OpenInference 属性不含——需 `enrich.py` 对每个 span 增补（对齐官方 experiments-via-OTel 三层模型）
3. **判分回填**：reward/reward-details → Scores API（NUMERIC），现象标签 → CATEGORICAL
4. **Dataset/Experiment 管理面**：对齐 LangSmithPlugin 的 dataset 同步 + experiment session

## 4. 工作量与计划

| PR | 内容 | 规模 | 周期 |
|---|---|---|---|
| PR-A | `uploaders/langfuse.py` + `_make_uploader` 自动探测（LANGFUSE_PUBLIC_KEY 存在即选 Langfuse）+ 单测 + README | ~150 行 | 1–2 天 |
| PR-B | `packages/harbor-langfuse`（plugin/enrich/scores）+ 文档 + golden 测试 | ~600 行 | 1–2 周 |
| 后续 RFC | 模型链路事件源（环境注入/反代）→ 真 TTFT/TPOT | 独立立项 | — |

PR 前置动作：订阅 harbor-framework 仓库 issue/PR 动态，确认无平行提案；RFC 先发 issue 讨论再提交代码。

## 5. 本地原型与上游的关系

本仓 `src/atif2langfuse/`（独立导出器）已端到端验证 trace 树/ Scores/脱敏，接口设计被 RFC 吸收；上游合并后，原型保留两个用途：① 离线补导历史 run；② structural 脱敏模式的参考实现（`sanitize.py`）。
