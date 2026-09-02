# 方案 A 部署与验证：LiteLLM 反代 + Langfuse（vllm-ascend + mini-swe-agent）

目标：五个指标在 Langfuse 上尽可能以真值呈现——① reward（真值）、② 轨迹质量四指标（真值）、③ 三段拆解（模型段真值）、④ TTFT/TPOT（proxy 真值）、⑤ per-tool 时延（近似）。

```
mini-swe-agent（OPENAI_BASE_URL 指向 proxy）
        │ 模型请求（含 streaming）
        ▼
LiteLLM Proxy :4000 ──OTel──► gen_ai.server.time_to_first_token / TPOT
        │                      token / cost
        ▼
Langfuse（Proxy Trace，每请求一条）
        ▲ sessionId（join 工具按时间窗 PATCH）
Harbor trial 产物 ──atif2langfuse export──► Trial Trace + reward scores
                              └─analyzer / join──► ②③⑤ scores
```

## 一、部署（内网 docker）

```bash
# 1. Langfuse 全栈（官方 compose）
git clone https://github.com/langfuse/langfuse.git && cd langfuse
cp docker-compose.yml docker-compose.override.yml   # 按官方指引改 db 密码
docker compose up -d
# 浏览器开 localhost:3000 注册 → 建 Project → Settings→API Keys 拿 pk/sk

# 2. 叠加 LiteLLM（本目录两个文件拷进 langfuse 目录）
cp <本目录>/docker-compose.litellm.yml <本目录>/litellm_config.yaml .
cp <本目录>/.env.example .env     # 填 VLLM_ASCEND_BASE_URL / 密钥 / LANGFUSE_AUTH_B64
# 生成 AUTH_B64：printf 'pk-lf-xxx:sk-lf-xxx' | base64 -w0
docker compose -f docker-compose.yml -f docker-compose.litellm.yml up -d

# 3. 连通性自测（流式！TTFT 仅流式请求上报非零值）
curl -N http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"ascend-qwen","stream":true,"messages":[{"role":"user","content":"ping"}]}'
# → Langfuse Traces 页应出现一条 proxy trace
```

`litellm_config.yaml` 两处需按实际改：`model_name`（agent 请求别名）与 `model: openai/<vllm serve 的模型名>`。

## 二、跑评测（mini-swe-agent 走 proxy）

```bash
export OPENAI_API_KEY=$LITELLM_MASTER_KEY
export OPENAI_BASE_URL=http://localhost:4000/v1
export OPENAI_MODEL=openai/ascend-qwen

harbor run -t hello-world -a mini-swe-agent -m openai/ascend-qwen \
  --trials-dir /root/runs/demo
# 容器内 agent 访问宿主机 proxy 时 base_url 用 http://host.docker.internal:4000/v1
```

**注意（TTFT 陷阱）**：mini-swe-agent 默认非流式调用 → TTFT 记 0、TPOT 仍有效（时长/token 均值口径）。要 TTFT 真值需确认 agent 开启 streaming；verify 脚本会显式检查并提示。

## 三、导出与五指标验证（按序执行）

```bash
cd /root/harbor-langfuse/deploy/scheme-a
set -a; source .env; set +a

# 1. Trial 轨迹 + reward → Trial Trace
atif2langfuse export --run-root $RUN_ROOT

# 2. ②轨迹质量 + ③task_duration + ⑤per-tool scores（可先 --dry-run 看数）
python3 tools/analyzer.py --run-root $RUN_ROOT

# 3. proxy trace join + ③三段拆解（model_time 真值 / non_model 残差）
python3 tools/join_proxy_traces.py --run-root $RUN_ROOT

# 4. 验收清单（五指标逐项 PASS/FAIL）
python3 tools/verify_langfuse.py --run-root $RUN_ROOT
```

## 四、指标口径（写报告时照抄）

| Score / 属性 | 口径 |
|---|---|
| `reward` / `reward.*` | Verifier 判分，真值 |
| `tool_calls_total` / `distinct_tools` / `redundant_call_ratio` / `loop_detected` | ATIF 轨迹客观统计，真值（冗余=同签名重复；循环=连续≥3 相同调用+相同结果哈希） |
| `tool_error_rate` | observation 命中错误模式正则的启发式，口径透明可配置 |
| `task_duration_s` | 轨迹首末时间戳，真值 |
| `model_time_s` | 窗口内 proxy trace latency 总和，真值（经 proxy 口径） |
| `non_model_time_s` | task_duration − model_time，残差（工具+overhead 合计） |
| `tool.<name>.interval_s` | 含该工具 step 的平均间隔，近似（含模型时间） |
| TTFT / TPOT | proxy 的 gen_ai.server.* 属性，流式真值 / 平均口径 |

**已知限制**：LiteLLM proxy 自身开销约 +40ms 量级（TTFT 需标注"经 proxy 口径"）；并发场景下 join 为时间分桶近似（验证期 C=1 精确）；⑤ 工具段执行时刻在 agent 进程内，proxy 不可见，永远为近似。

## 文件清单

- `docker-compose.litellm.yml` / `litellm_config.yaml` / `.env.example` — 部署
- `tools/lf_client.py` — Langfuse REST 客户端（共享）
- `tools/analyzer.py` — ②③⑤离线分析 → scores
- `tools/join_proxy_traces.py` — proxy trace 关联 + 三段拆解
- `tools/verify_langfuse.py` — 五指标验收清单
