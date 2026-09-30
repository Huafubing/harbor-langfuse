# Harbor × Langfuse 演示执行指导（Runbook）

> 更新：2026-09-04（全部命令均实测通过）
> 全流程：`harbor run`（3 个 case 串行）→ `atif2langfuse export`（上传 trace + reward）→ `backfill_dashboard_scores.py`（回填 21 个 Dashboard score）→ 验证 22 scores/trace → 刷新 Dashboard
> 拉起支持两种等价方式：**方式 A 纯命令行**（便于逐行讲解参数）/ **方式 B yaml 配置文件**（`harbor_langfuse_demo.yaml` + `-c` 参数）。浏览器看 Dashboard 前需先做 §2 端口转发。

---

## 0. 环境与凭据

| 项 | 值 |
|---|---|
| conda 环境 | `harbor1`（含 harbor、atif2langfuse、requests） |
| Langfuse | http://localhost:3000 ，项目 `harbor-trial`，v4.26 |
| Langfuse PK | `pk-lf-70bd50ec-9ad1-41c3-b60b-76077eb6fbb6` |
| Langfuse SK | `sk-lf-d67e036b-9a6b-4d7c-8306-c1ccdb59c9df` |
| litellm proxy | http://127.0.0.1:4000/v1 ，`OPENAI_API_KEY=sk-master-2cece7efb0a249859bfd5b2f2bc31f0f` |
| harbor 源码 | `/home/yanhe/yh_dev/harbor`（editable 安装） |
| 数据集 | `/home/yanhe/swebench-verified` |
| agent 依赖包 | `/home/yanhe/mini-swe-agent-ubuntu2204.tar.gz` |
| 回填脚本 | `/home/yanhe/harbor-langfuse/scripts/backfill_dashboard_scores.py` |
| 演示配置 | `/home/yanhe/harbor_langfuse_demo.yaml`（方式 B 用） |
| 演示 case（本地均有镜像） | `django__django-12708`、`django__django-11163`、`astropy__astropy-13033` |

---

## 1. Step 0 前置检查

每个新终端先执行：

```bash
conda activate harbor1
export LF_PK='pk-lf-70bd50ec-9ad1-41c3-b60b-76077eb6fbb6'
export LF_SK='sk-lf-d67e036b-9a6b-4d7c-8306-c1ccdb59c9df'
export RUN_ROOT=/home/yanhe/jobs/harbor_langfuse_demo
```

服务与镜像检查：

```bash
curl -s http://localhost:3000/api/public/health     # Langfuse 存活
curl -s http://localhost:4000/health/liveliness     # litellm proxy 存活
docker images | grep -E 'django-12708|django-11163|astropy-13033'   # 3 个本地镜像在
```

以上检查均在**服务器本机**执行即可；浏览器访问 Langfuse 需先完成 §2 的端口转发。

---

## 2. Langfuse 网页端口转发（浏览器访问 Dashboard 必做）

Langfuse 只监听在服务器本机 `localhost:3000`。终端里的 curl / export / 回填都在服务器上执行、不受影响；但**自己电脑的浏览器**无法直接访问，需要先建 SSH 隧道把 3000 端口转发到本地。

### 方式 A：SSH 本地转发（推荐）

在**自己电脑**上另开一个终端：

```bash
ssh -N -L 3000:localhost:3000 yanhe@<服务器IP或主机名>
# Windows PowerShell 自带 OpenSSH，命令相同
```

- `-L 3000:localhost:3000`：把本机 3000 端口映射到服务器的 `localhost:3000`
- `-N`：只转发端口、不登录执行命令；**窗口保持开着**即隧道生效
- 需要顺带看 litellm 时可加 `-L 4000:localhost:4000`（一般演示不需要）
- 想放后台：`ssh -fN -L 3000:localhost:3000 yanhe@<服务器IP或主机名>`（演示结束 `pkill -f "ssh -fN -L 3000"` 关闭）

之后浏览器打开 http://localhost:3000 登录即可。

### 方式 B：IDE Remote-SSH 端口面板

用 VS Code / Trae 的 Remote-SSH 连上服务器后，底部 **PORTS/端口** 面板 → 转发端口 → 填 `3000`，随后按 IDE 提示的本地地址访问（通常是 http://localhost:3000）。

### 排查

- 隧道已建但打不开：先在服务器上确认 `curl -s http://localhost:3000/api/public/health` 正常（§1）；
- 本机 3000 被占用：换本地端口，如 `-L 3001:localhost:3000`，浏览器访问 http://localhost:3001。

---

## 3. Step 1 harbor run（两种方式二选一）

### 方式 A：纯命令行（便于逐行讲解参数）

```bash
cd ~
harbor run \
  --job-name harbor_langfuse_demo \
  -n 1 \
  -p /home/yanhe/swebench-verified \
  -i django__django-12708 \
  -i django__django-11163 \
  -i astropy__astropy-13033 \
  -a mini-swe-agent \
  -m openai/deepseek-v4-flash \
  --ak 'config={"model":{"model_class":"litellm"}}' \
  --ae 'OPENAI_API_KEY=sk-master-2cece7efb0a249859bfd5b2f2bc31f0f' \
  --ae 'OPENAI_BASE_URL=http://127.0.0.1:4000/v1' \
  --agent-deps /home/yanhe/mini-swe-agent-ubuntu2204.tar.gz \
  -e docker \
  --host-network \
  --no-delete \
  -o jobs
```

逐行参数解释（演示讲解用）：

| 参数 | 含义 |
|---|---|
| `--job-name harbor_langfuse_demo` | 任务名，决定输出目录 `jobs/harbor_langfuse_demo/` |
| `-n 1` | 并发 1，3 个 case 串行跑，Langfuse 时间线不交叠 |
| `-p /home/yanhe/swebench-verified` | 数据集根目录 |
| `-i <task>`（可重复，×3） | 只跑指定 task；3 个均确认本地有 swebench 镜像 |
| `-a mini-swe-agent` | agent 名 |
| `-m openai/deepseek-v4-flash` | 模型，走 litellm |
| `--ak 'config={"model":{"model_class":"litellm"}}'` | agent kwargs；**value 整串按 JSON 解析**，嵌套结构必须写成 JSON 字符串（等价 yaml 的 `config.model.model_class: litellm`） |
| `--ae 'KEY=VALUE'`（可重复，×2） | 注入 agent 环境变量：API key + litellm 地址 |
| `--agent-deps ...tar.gz` | agent 依赖离线包 |
| `-e docker` | 执行环境用 docker |
| `--host-network` | 容器用宿主网络，容器内才能访问 `127.0.0.1:4000` 的 litellm |
| `--no-delete` | 跑完不删产物，`trajectory.json` 保留给 export 用 |
| `-o jobs` | 输出根目录（相对当前目录，`cd ~` 后即 `~/jobs`） |

### 方式 B：yaml 配置文件

`harbor_langfuse_demo.yaml` 与方式 A 完全等价（job_name / n_concurrent_trials / environment / agents / datasets 一一对应，已用 `--print-config` 与命令行版比对一致）：

```bash
cd ~
harbor run -c /home/yanhe/harbor_langfuse_demo.yaml
# 显式指定输出目录（不指定时默认就是 jobs，相对当前目录）：
harbor run -c /home/yanhe/harbor_langfuse_demo.yaml -o jobs
```

- `-c/--config` 接受 yaml/json 路径（schema 为 `harbor.models.job.config.JobConfig`）
- `-o/--jobs-dir` 默认 `jobs`，`cd ~` 执行后输出同样落在 `~/jobs/harbor_langfuse_demo/`，与方式 A 汇合到同一 RUN_ROOT
- CLI 参数可与 `-c` 混用并覆盖 yaml 同名配置（如临时换 `--job-name` 加日期后缀）

---

## 4. Step 2 确认产物与 RUN_ROOT

**注意目录结构**：trial 目录直接放在 job 目录下（没有中间时间戳层）：

```
/home/yanhe/jobs/harbor_langfuse_demo/
├── django__django-12708__Qqj3ocV/agent/trajectory.json
├── django__django-11163__6A8nDKe/agent/trajectory.json
└── astropy__astropy-13033__QMF8nLh/agent/trajectory.json
```

```bash
find "$RUN_ROOT" -name trajectory.json | sort   # 应有 3 条
```

---

## 5. Step 3 上传 trace

```bash
atif2langfuse export \
  --run-root "$RUN_ROOT" \
  --host http://localhost:3000 \
  --pk "$LF_PK" \
  --sk "$LF_SK"
# 期望：发现 Trial：3，每条 [exported] + [score:ok] reward，共 ~数百 span
```

说明：
- export 只上传 trace + 1 个 `reward` score；其余 21 个 score 靠 Step 4 回填。
- 只想补传单个 trial 时，`--run-root` 指向该 trial 目录即可。

---

## 6. Step 4 回填 21 个 Dashboard score

先 dry-run（只打印不上传）：

```bash
cd /home/yanhe/harbor-langfuse
python scripts/backfill_dashboard_scores.py \
  --run-root "$RUN_ROOT" --host http://localhost:3000 \
  --pk "$LF_PK" --sk "$LF_SK" --dry-run
# 期望：3 个 trial 指标齐全，最后 DRY-RUN 结束：全部成功
```

确认后正式回填（**幂等**：同 trace 同 name 使用确定性 id，重复执行自动覆盖更新，不会产生重复行）：

```bash
python scripts/backfill_dashboard_scores.py \
  --run-root "$RUN_ROOT" --host http://localhost:3000 \
  --pk "$LF_PK" --sk "$LF_SK"
# 期望：每个 trial 打印 上传完成：21/21 个 score
```

---

## 7. Step 5 验证（每个 trace 应有 22 个 score）

```bash
for TID in <trace_id_1> <trace_id_2> <trace_id_3>; do
  curl -s -u "$LF_PK:$LF_SK" "http://localhost:3000/api/public/traces/$TID" \
    | python -c "import sys,json;d=json.load(sys.stdin);print(d['name'], len(d['scores']))"
done
# 期望 3 行，各 <task>#1 22
```

注意：score 写入是**异步入库**的（POST 返回 200 后几秒才可见），回填后立即验证若不足 22，等几秒重跑本步即可；若 trace 保留了历史重复行（见踩坑备忘第 12 条），计数会**大于** 22，属预期。

trace id 的获取方式（二选一）：
- Step 3/4 的输出里会打印每个 trial 的 trace id；
- 或按 trace 名反查（`#` 要编码为 `%23`，见踩坑备忘第 6 条）：

```bash
curl -s -u "$LF_PK:$LF_SK" -G "http://localhost:3000/api/public/traces" \
  --data-urlencode "name=django__django-12708#1" --data-urlencode "limit=10" \
  | python -c "import sys,json;[print(t['id'],t['name'],t['timestamp']) for t in json.load(sys.stdin)['data']]"
```

---

## 8. Step 6 查看 Dashboard 与指标讲解（先完成 §2 端口转发）

浏览器打开 http://localhost:3000 → 项目 `harbor-trial` → Dashboards → **Harbor · 时延性能**；右上角时间范围选 Past 1 day，experiment 过滤勾选后应看到本轮 3 个 trial。
除 Dashboard 外，**Tracing → 点开任一 `<task>#1` trace → 详情页 Scores 列表**可看该 trial 的全部 22 个 score（Observability → Scores 页则可按 name 横向对比）。

### 8.1 Dashboard 布局与讲解动线

| 部件 | 内容 | 讲解要点 |
|---|---|---|
| ① 总体卡片 ×4 | 任务时长 / 模型段 / 非模型段 / LLM 请求时延（均值） | 30 秒建立整体画像：跑了多久、时间花在哪、单次请求多快 |
| ② 时间构成 饼图 | 模型段 vs 非模型段 占比（sum 聚合） | 回答"时间花在哪"：模型思考 vs 工具执行 + 框架开销 |
| ③ 各 Trial×三段 透视表 | 行=Trial，列=task/model/non_model | 下钻单 trial 对比，哪个 trial 模型占比异常一目了然 |
| ④ LLM 时延面板 | avg / p95 / TTFT / TPOT 横条 | 请求级性能与流式体验；p95 远超 avg 说明存在长尾慢请求 |
| ⑤ 生成吞吐 / Token | 各 Trial tput 柱图；tokens in/out/合计 | 解码吞吐与上下文规模（成本侧视角） |
| ⑥ 请求量 / 工具 | 各 Trial LLM 请求数；per-tool 时延 | 调用频次（干了多少活）与工具耗时分布 |

建议讲解动线：**① 整体画像 → ②③ 时间构成（时间花哪了）→ ④⑤ LLM 性能（快不快、稳不稳）→ ⑥ 行为频次（忙不忙、有没有打转）**。

### 8.2 指标清单（每 trial 22 个 score，按关注维度分组）

**A. 结果质量**（export 写入，来自 harbor 评测）

| 指标 | 口径 | 关注点 |
|---|---|---|
| `reward` | 评测得分 | 最终"做没做对"；务必与耗时分开设问，防止"又快又错" |
| `reward.*` / `phenotype` | trial 带 reward_details / phenotypes 时 export 额外写入 | 细分项得分与标签（本 demo 3 题均只有总 reward） |

**B. 任务时间构成**（回填脚本）

| 指标 | 口径 | 关注点 |
|---|---|---|
| `task_duration_s` | 首末 agent step 时间差（墙钟） | 任务总耗时 |
| `model_time_s` | 时间窗内全部 litellm GENERATION 延迟之和（proxy 真值） | 花在模型上的时间 |
| `non_model_time_s` | task − model | 工具执行 + agent 框架开销；占比高 → 瓶颈在环境/工具侧而非模型 |

**C. LLM 性能**（litellm GENERATION 聚合，proxy 真值）

| 指标 | 口径 | 关注点 |
|---|---|---|
| `llm_requests` | GENERATION 条数 | 调用频次；与 steps_total 对比看单步是否多发请求 |
| `llm_latency_avg_s` / `llm_latency_p95_s` | 单请求延迟均值 / p95 | 平均水平与长尾；本 demo 约 2.4~6.4s |
| `llm_ttft_avg_s` | 流式首 token 时延（原生 completion_start_time 真值） | 排队 + 预填充耗时，决定"响应快不快" |
| `llm_tpot_avg_s` | 流式解码阶段均摊每 token 时长 | 生成（decode）速度；TTFT 缺失时退化为时长/token 均值；本 demo 0.010~0.023s/token |
| `llm_tput_tokens_per_s` | output tokens / model_time | 端到端吞吐 |
| `llm_tokens_input` / `output` / `total` | proxy usage token 数 | 上下文规模与成本；input 大 → 长 prompt/多轮累积 |

**D. Agent 行为**（回填脚本统计 trajectory.json）

| 指标 | 口径 | 关注点 |
|---|---|---|
| `steps_total` | agent 步数 | 任务复杂度 / 是否啰嗦 |
| `tool_calls_total` / `tool.bash.calls` | 工具调用总数 / 其中 bash | 干了多少活、bash 占比 |
| `tool_errors_total` / `tool_error_rate` | observation 中 returncode≠0 的次数 / 占比 | 工具失败率；偏高 → 环境难或 agent 在乱试 |
| `distinct_tools` | 去重工具数 | 行为面宽度 |
| `tool.bash.interval_s` | 相邻两次 bash 平均间隔 | 观察/思考节奏 |
| `redundant_call_ratio` | 重复出现的相同命令数 / 总调用数 | 是否在原地打转 |
| `loop_detected` | 存在 ≥3 次重复同一命令 → "1"（CATEGORICAL） | 最直白的打转信号，可当过滤器快速定位问题 trial |

### 8.3 数据从哪来（两条链路、三层含义）

- **trajectory.json → export**：上传 trace/span + reward（A 组）；回填脚本统计行为类 score（B 组 task_duration、D 组）；
- **litellm proxy 实时上报**：GENERATION 挂在 litellm 自建 trace 上（异步入库）→ 回填脚本按 trial 时间窗聚合出 model_time 与 C 组真值；
- 因此 **A 组 = 评测结论、B/C 组 = proxy 观测真值、D 组 = trajectory 复盘统计**，三层互相印证：reward 回答"对不对"，时间/LLM 指标回答"贵不贵、快不快"，行为指标回答"过程健不健康"。

---

## 9. 清理与重演

### 9.1 一键清理本轮全部 trace（重传前先执行）

按 3 个 trace name 把项目里的相关 trace 全部查出、逐条删除，再统一验证 404（Basic auth；DELETE 是**异步**的，见踩坑备忘第 3 条）：

> **安全边界**：本清理只删 name 为 `xxx#1` 的 harbor trial trace，**不会动 litellm proxy 上报的 `litellm-acompletion` trace**——model_time/llm_* 的时延真值（GENERATION）就挂在那批 trace 上，trajectory.json 里没有、export 不会重建。一旦删掉只能重跑 `harbor run` 才能找回，**切勿在 Tracing 页面全选删除**。

```bash
# 1) 按 name 查出全部相关 trace id（重演过的旧 trace 也会一并清掉）
: > /tmp/lf_trace_ids.txt
for NAME in django__django-12708#1 django__django-11163#1 astropy__astropy-13033#1; do
  curl -s -u "$LF_PK:$LF_SK" -G "http://localhost:3000/api/public/traces" \
    --data-urlencode "name=$NAME" --data-urlencode "limit=50" \
    | python -c "import sys,json;[print(t['id']) for t in json.load(sys.stdin)['data']]" \
    >> /tmp/lf_trace_ids.txt
done
sort -u -o /tmp/lf_trace_ids.txt /tmp/lf_trace_ids.txt
cat /tmp/lf_trace_ids.txt   # 应为 3 行；重演过多轮则会更多

# 2) 循环删除
while read -r TID; do
  curl -s -o /dev/null -w "DELETE $TID -> %{http_code}\n" -u "$LF_PK:$LF_SK" \
    -X DELETE "http://localhost:3000/api/public/traces/$TID"
done < /tmp/lf_trace_ids.txt

# 3) 等 8 秒后验证全部 404
sleep 8
while read -r TID; do
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -u "$LF_PK:$LF_SK" \
    "http://localhost:3000/api/public/traces/$TID")
  echo "$TID -> $CODE"   # 期望全部 404
done < /tmp/lf_trace_ids.txt
```

清理完成后回到 Step 3（§5）重新 export、Step 4（§6）重新回填即可，界面即恢复为新一轮的 3 条 trace。

### 9.2 其他重置方式

- **删某条 trace**（只想清一条时）：

  ```bash
  curl -s -o /dev/null -w '%{http_code}\n' -u "$LF_PK:$LF_SK" \
    -X DELETE "http://localhost:3000/api/public/traces/<trace_id>"
  ```

- **重跑同一批数据**：export 会重复建 trace，需先执行 9.1 清理再导；backfill 脚本本身幂等，可直接重跑。
- **演示全新一轮**：换个 `--job-name`（如加日期后缀）重跑 Step 1 起。

---

## 10. 踩坑备忘（演示前过一遍）

1. **REST API 只认 Basic auth（pk:sk）**，`Authorization: Bearer sk-...` 返回 401。curl 用 `-u "$LF_PK:$LF_SK"`。
2. **traces 列表接口（GET /api/public/traces）不可信**：`fromStartTime`/`toStartTime` 过滤无效（返回全项目）；`observations` 字段只是 **id 字符串数组**，不是对象数组。查 root span / scores 必须走详情接口 `GET /api/public/traces/{id}`。
3. **DELETE trace 是异步的**：返回 200 后立刻 GET 可能仍是 200，等几秒再确认 404。
4. `--ak` 是唯一易翻车参数：等号左边是 key，右边整串按 JSON 解析；不要写 `config.model.model_class=litellm` 的点号平铺（会生成字面量 key）。
5. **必须 `--no-delete`**：否则跑完产物被清，`trajectory.json` 没了无法 export。
6. URL 里 trace name 的 `#` 必须编码为 `%23`（或用 curl `-G --data-urlencode`），否则被当成 URL fragment。
7. `RUN_ROOT` 指向 **job 目录**（`~/jobs/harbor_langfuse_demo`），一次导出/回填全部 trial；指向单个 trial 目录则只处理该条。
8. score 口径：`model_time_s` = Langfuse 内 litellm GENERATION 延迟之和（proxy 真值）；`non_model_time_s` = task_duration − model_time；`loop_detected` 是 CATEGORICAL（"0"/"1" 字符串），其余 20 个 NUMERIC。
9. score 结构：每 trial 22 个 = export 写的 `reward` + 脚本回填的 21 个。
10. **litellm GENERATION 是异步入库的**：`harbor run` 刚结束时，litellm 回调可能还没把 GENERATION 灌完，此时回填会报"时间窗内未查到 litellm GENERATION"而整批跳过。等几分钟再 dry-run，确认 `llm_requests` 与步数量级一致后再正式回填。
11. **不要删除 `litellm-acompletion` 开头的 trace**：它们是 litellm proxy 实时上报的（不在 trajectory.json 里），删了 model_time/llm_* 永远无法回填，只能重跑 harbor run。
12. **v4.26 的 `DELETE /api/public/scores/{id}` 只返回 202 排队、队列实际不消费**（等数分钟都不生效），"先删后写"式幂等会积累重复 score 行。回填脚本已改用确定性 id（`uuid5(trace_id, name)`）upsert 覆盖；若历史已产生重复行，AVG 聚合值几乎不受影响（重复值相同），介意干净就走 §9.1 重置重演。

---

## 11. 参考：2026-09-04 首演实测数据

| trial | trace_id | reward 之外关键指标 |
|---|---|---|
| astropy__astropy-13033__QMF8nLh | `6463e08c60650540e2b87aa5d8e2ee04` | steps=37, tool_errors=6/37, task_dur=257.651s, model_time=235.762s, llm_requests=37 |
| django__django-11163__6A8nDKe | `23dfd47d11de9a48ac989dcdd78e1974` | steps=46, tool_errors=11/52, task_dur=218.253s, model_time=108.113s, llm_requests=45 |
| django__django-12708__Qqj3ocV | `943ac66f23d3a46d383af16c80bff08d` | steps=55, tool_errors=7/61, task_dur=413.257s, model_time=358.252s, llm_requests=56 |

完整 dry-run 输出见 Step 4（含 ttft、p95、tpot、tokens、tput 等全部 21 个 score 值）。
