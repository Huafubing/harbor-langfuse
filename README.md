# atif2langfuse — Harbor ATIF 轨迹接入 Langfuse

把 [Harbor](https://www.harborframework.com) 评测框架每次 Trial 产出的 ATIF 轨迹与 Verifier 判分，导出到**自托管 Langfuse**，实现"评测跑完即可复盘与横评"。对应方案文档《Harbor-ATIF-Langfuse接入方案.md》的可运行实现。

一句话架构：

```
Harbor run 产物目录                      导出器 atif2langfuse                Langfuse(自托管 v4)
├─ <trial>/agent/trajectory.json ──►  读取层(复用/兼容 traces_utils)
├─ <trial>/verifier/reward.*      ──►  脱敏层(full|structural)  ──OTLP/HTTP──►  Trace 树(Generation/Tool)
└─ <trial>/result.json            ──►  span 构造层(OTel SDK)    ──Scores API──► Scores / Experiment 看板
```

## 环境要求

- Linux / WSL2（Ubuntu 已验证），Python >= 3.9
- 自托管 Langfuse >= 3.22（推荐 v4），OTel 端点 `/api/public/otel`
- 上报依赖：`opentelemetry-sdk`、`opentelemetry-exporter-otlp-proto-http`（`list` 与 `--dry-run` 只用标准库，可不装）

## 第一步：部署 Langfuse（已有实例可跳过）

```bash
git clone https://github.com/langfuse/langfuse.git
cd langfuse
docker compose up -d          # 本地快速起全栈，默认端口 3000
```

打开 `http://localhost:3000`，注册组织 → 创建 Project → Settings → API Keys，生成 `pk-lf-...` 与 `sk-lf-...`。生产部署（Postgres/MinIO/S3、鉴权密钥等）参考官方 self-hosting 文档，不要直接用示例密钥上线。

## 第二步：安装导出器

本目录即项目根（`/root/harbor-langfuse`）：

```bash
cd /root/harbor-langfuse
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[otel]"
cp .env.example .env          # 填入 LANGFUSE_HOST / PK / SK
set -a; source .env; set +a   # 载入环境变量（或用 CLI 参数代替）
```

没有 pip 网络/不想装依赖时，`list` 和 `--dry-run` 可直接用标准库运行：

```bash
PYTHONPATH=src python3 -m atif2langfuse list --run-root examples/demo-run
```

## 第三步：识别 Harbor 产物

工具按 Harbor 主线约定发现 Trial：**目录下存在 `agent/trajectory.json` 即视为一个 Trial**。目录名按 `task__agent__model__attempt` 回退解析；`result.json` 的字段（agent_name/model_name/task_name/run_id/reward）优先级更高。判分读取顺序：`verifier/reward.txt` → 缺省回退 `verifier/reward.json`；分维度得分读 `verifier/reward-details.json`。

```
runs/                                ← --run-root 指向这里
└─ hello-sql__terminus-2__openai_gpt-4o-mini__1/
   ├─ agent/trajectory.json          # ATIF v1.8 轨迹
   ├─ result.json                    # 运行元数据
   └─ verifier/
      ├─ reward.txt | reward.json    # 总分
      └─ reward-details.json         # 逐判据得分（Reward Kit）
```

如果 Python 环境里装了 Harbor 源码包，读取层会自动 `import harbor.utils.traces_utils` 复用其发现与元数据逻辑；没装则用内置兼容实现（`reader.py`），两者行为一致。

## 快速开始

```bash
# 1) 看看能发现哪些 Trial、判分是多少
atif2langfuse list --run-root /path/to/runs

# 2) 干跑：构建 span 计划但不联网（验证解析、脱敏、span 数量）
atif2langfuse export --run-root /path/to/runs --dry-run

# 3) 正式导出 + 回填 Scores
atif2langfuse export --run-root /path/to/runs

# 常用变体
atif2langfuse export --run-root ... --filter failure        # 只导失败样本做归因
atif2langfuse export --run-root ... --skip-scores           # 只导轨迹，不回填判分
atif2langfuse export --run-root ... --mode full             # 原文上报（仅内网调试）
atif2langfuse export --run-root ... --dataset-id <id> \
                    --run-name "昇腾横评-第1轮"               # 关联 Dataset / 实验命名
```

仓库自带一个最小 Trial（`examples/demo-run`），可直接跑通全流程。

## 自测

```bash
python3 tests/test_execute_plan.py   # 离线断言 span 树结构/属性/脱敏（无需 Langfuse 与网络）
python3 tests/test_provider.py       # 验证 OTLP exporter 构造与请求头
```

## 数据映射速查

| Harbor / ATIF | Langfuse | 关键属性 |
|---|---|---|
| Run | Experiment | `langfuse.experiment.id/name`（baggage 语义，打到每个 span） |
| Task | Experiment Item | root span：`langfuse.experiment.item.id`、`root_observation_id` |
| Trial | Trace（root span `trial`） | `langfuse.session.id`、`langfuse.trace.tags=["harbor", agent, model]` |
| agent Step | Generation span `llm` | `gen_ai.request.model`、`gen_ai.usage.input_tokens/output_tokens`、cached_tokens、`atif.cost_usd` |
| ToolCall | Tool span `tool.<name>` | input=arguments（脱敏）、output=observation |
| user/system Step | root 上的 Event | `step.<id>.<source>` |
| reward.txt/json | Score `reward` (NUMERIC) | 挂 trace root |
| reward-details.json | Score `reward.<criterion>` | 挂 trace root |
| E1–E7 现象标签 | Score `phenotype` (CATEGORICAL) | 从 trajectory `extra.phenotypes` 或 result.json `phenotypes` 读取 |
| final_metrics | trace metadata `total_*` | 总 tokens/cost/steps |

## 脱敏模式

- `structural`（默认）：字符串叶子值替换为 `{"_len": 24, "_sha256_8": "3f2a91bc"}`，保留字段名、token 数、工具名、判分——"保结构弃内容"，满足数据不出域。
- `full`：原文上报（超长截断到 64KB），仅限内网调试。

## 在 Langfuse 里看什么

- **Traces**：按 `tags` 过滤 `harbor` + agent + model；Trace 树为 root(trial) → llm / tool.*，root 的 input/output 即任务指令与最终回复（脱敏后为哈希摘要）。
- **Scores**：`reward`、`reward.schema/coverage/...` 可直接做聚合看板与跨底座对比。
- **Experiments**：同一 `run_id` 的全部 Trial 聚成一次实验，多轮 run 横比 pass rate 与 token 成本。

## 常见问题

| 现象 | 原因与处理 |
|---|---|
| 401/403 | pk/sk 错误或 host 不对；确认 Basic Auth 用 `pk-lf-xxx:sk-lf-xxx` |
| 数据延迟最多 10 分钟 | 直连 OTel 必须带 `x-langfuse-ingestion-version: 4`，本工具已自动携带 |
| `phenotype` 分值不显示 | CATEGORICAL 需先在 UI 建同名 score config，再重导 |
| span 没出现在 Trace | root span 必须上报；本工具保证 root 必发，不做 span 级过滤 |
| OTLP 报连接错误 | Langfuse 仅支持 OTLP/HTTP（不支持 gRPC），确认 host 可达 |
| result.json 字段对不上 | 不同版本字段名有差异，改 `reader.py` 的 `_meta_from_result` 一处即可 |
| 搜 OTel 配置搜到 goharbor | 那是同名容器镜像仓库项目，与评测框架 Harbor 无关 |

## 局限与路线图

- ATIF 无原生 TTFT/TPOT，Generation/Tool span 时长为相邻 step 间隔近似值；真值需接入结构轨 Sidecar（同一 OTel Collector，`langfuse.session.id` 自动聚合，对应方案文档第二阶段）。
- `subagent_trajectories` 目前聚合为汇总 span，不递归展开子轨迹树。
- 稳定后可将本工具封装为 Harbor `export-otel` 子命令回馈上游（主线目前无 OTel 能力）。

## 项目结构

```
harbor-langfuse/
├── pyproject.toml            # 依赖与入口（atif2langfuse 命令）
├── .env.example              # 配置模板
├── src/atif2langfuse/
│   ├── cli.py                # list / export 子命令
│   ├── config.py             # 环境变量 + CLI 覆盖
│   ├── reader.py             # Trial 发现与 ATIF/判分解析（兼容 traces_utils）
│   ├── sanitize.py           # structural|full 内容模式
│   ├── spans.py              # 计划层（可 dry-run）+ OTel 执行层
│   └── scores.py             # Scores API 回填
├── tests/                    # 离线自测（span 树结构 / provider 构造）
└── examples/demo-run/        # 最小 Trial 样例，开箱即测
```

## Roadmap

- [x] v0.1.0 独立导出器（本仓 `src/atif2langfuse/`，已端到端验证）
- [ ] RFC 0001 上游化：`LangfuseUploader` + `LangfusePlugin` 提回 harbor-framework → [docs/rfcs/0001-langfuse-integration.md](docs/rfcs/0001-langfuse-integration.md)（上游接口形态分析见 [docs/upstream-interface-analysis.md](docs/upstream-interface-analysis.md)）
- [ ] 时延真值事件源（环境注入 / 模型反代，后续 RFC，参考上游 `environments/langsmith.py` 模式）
