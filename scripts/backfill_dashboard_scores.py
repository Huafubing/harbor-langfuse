#!/usr/bin/env python
"""Harbor Trial -> Langfuse Dashboard score 回填脚本.

在 `atif2langfuse export` 之后执行：export 只上传 reward 一个 score，
本脚本把 Dashboard（"Harbor · 时延性能"）依赖的其余 21 个 score 挂到对应 trace 上：

  task_duration_s / model_time_s / non_model_time_s
  llm_requests / llm_latency_avg_s / llm_latency_p95_s / llm_ttft_avg_s
  llm_tpot_avg_s / llm_tput_tokens_per_s
  llm_tokens_input / llm_tokens_output / llm_tokens_total
  steps_total / tool_calls_total / tool.bash.calls / tool_errors_total / tool_error_rate
  distinct_tools / tool.bash.interval_s / redundant_call_ratio / loop_detected(CATEGORICAL)

口径（与历史 django trial 一致）:
  - model_time_s        = Langfuse 内该 trial 时间窗内全部 litellm GENERATION 延迟之和（proxy 真值）
  - non_model_time_s    = task_duration_s - model_time_s
  - llm_tput_tokens_per_s = output_tokens / model_time_s
  - tool_error          = bash observation 内容中 returncode != 0
  - redundant_call_ratio= 重复出现的相同命令数 / 总工具调用数
  - loop_detected       = 存在重复 >= 3 次的相同命令时为 "1"（CATEGORICAL），否则 "0"

用法（在 atif2langfuse export 之后）:
  python backfill_dashboard_scores.py --run-root jobs/<run>/<ts> \
      --host http://localhost:3000 --pk pk-lf-... --sk sk-lf-...
  加 --dry-run 只计算打印，不上传。
"""

import argparse
import datetime as dt
import json
import re
import statistics as st
import sys
import uuid
from collections import Counter
from pathlib import Path

import requests

# Dashboard 依赖、且需要本脚本回填的 score 名称
NUMERIC_SCORES = [
    "task_duration_s", "model_time_s", "non_model_time_s",
    "llm_requests", "llm_latency_avg_s", "llm_latency_p95_s",
    "llm_ttft_avg_s", "llm_tpot_avg_s", "llm_tput_tokens_per_s",
    "llm_tokens_input", "llm_tokens_output", "llm_tokens_total",
    "steps_total", "tool_calls_total", "tool.bash.calls",
    "tool_errors_total", "tool_error_rate",
    "distinct_tools", "tool.bash.interval_s",
    "redundant_call_ratio",
]
LOOP_SCORE = "loop_detected"  # CATEGORICAL: "0"/"1"


def pt(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def discover_trials(run_root: Path):
    """发现 run_root 下所有 trial（以 agent/trajectory.json 为标记）。"""
    trials = []
    for d in sorted(p for p in run_root.iterdir() if p.is_dir()):
        traj = d / "agent" / "trajectory.json"
        if traj.exists():
            trials.append(d)
    return trials


def trial_task_name(trial_dir: Path) -> str:
    """从 trial 级 result.json 读 task_name，失败则从目录名截取。"""
    rj = trial_dir / "result.json"
    if rj.exists():
        try:
            name = json.load(open(rj)).get("task_name")
            if name:
                return name
        except Exception:
            pass
    return trial_dir.name.rsplit("__", 1)[0]


def trajectory_metrics(trial_dir: Path):
    """从 trajectory.json 计算 steps / tool 系列指标。"""
    traj = json.load(open(trial_dir / "agent" / "trajectory.json"))
    asteps = [s for s in traj["steps"] if s.get("source") == "agent"]
    t0, t1 = pt(asteps[0]["timestamp"]), pt(asteps[-1]["timestamp"])
    task_dur = (t1 - t0).total_seconds()

    calls = []
    for s in asteps:
        for c in s.get("tool_calls") or []:
            calls.append({
                "ts": pt(s["timestamp"]),
                "fn": c.get("function_name") or "unknown",
                "cmd": (c.get("arguments") or {}).get("command", ""),
            })

    # tool error: observation.results[].content 中的 returncode
    errors = 0
    for s in asteps:
        try:
            for r in s["observation"]["results"]:
                content = r.get("content")
                try:
                    body = json.loads(content)
                except Exception:
                    m = re.search(r'"returncode"\s*:\s*(-?\d+)', str(content))
                    body = {"returncode": int(m.group(1)) if m else 0}
                if body.get("returncode", 0) != 0:
                    errors += 1
        except Exception:
            pass

    bash = [c for c in calls if c["fn"] == "bash"]
    gaps = [(bash[i + 1]["ts"] - bash[i]["ts"]).total_seconds()
            for i in range(len(bash) - 1)]
    cnt = Counter(c["cmd"] for c in calls)
    dups = sum(v - 1 for v in cnt.values() if v > 1)

    return {
        "task_duration_s": round(task_dur, 3),
        "steps_total": len(asteps),
        "tool_calls_total": len(calls),
        "tool.bash.calls": len(bash),
        "tool_errors_total": errors,
        "tool_error_rate": round(errors / len(calls), 4) if calls else 0,
        "distinct_tools": len({c["fn"] for c in calls}),
        "tool.bash.interval_s": round(st.mean(gaps), 3) if gaps else 0.0,
        "redundant_call_ratio": round(dups / len(calls), 4) if calls else 0,
        "loop_detected": "1" if max(cnt.values(), default=0) >= 3 else "0",
    }


def llm_metrics(api, start: str, end: str):
    """查询 trial 时间窗内 litellm GENERATION（proxy 真值）并聚合。"""
    w0 = (pt(start) - dt.timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    w1 = (pt(end) + dt.timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    obs, page = [], 1
    while True:
        r = api.s.get(f"{api.host}/api/public/observations", params={
            "name": "litellm-acompletion", "type": "GENERATION",
            "fromStartTime": w0, "toStartTime": w1,
            "limit": 100, "page": page}, timeout=60).json()
        obs += r["data"]
        meta = r.get("meta", {})
        if page * meta.get("pageSize", 100) >= meta.get("totalItems", 0):
            break
        page += 1

    gens = []
    for o in obs:
        if not o.get("completionStartTime"):
            continue
        s_, c_, e_ = pt(o["startTime"]), pt(o["completionStartTime"]), pt(o["endTime"])
        u = o.get("usage") or {}
        out = u.get("output", 0)
        # TPOT = 流式解码阶段均摊每 token 时长（proxy 真值）；无输出 token 时跳过
        tpot = (e_ - c_).total_seconds() / out if out > 0 else None
        gens.append({
            "lat": (e_ - s_).total_seconds(),
            "ttft": (c_ - s_).total_seconds(),
            "tpot": tpot,
            "inp": u.get("input", 0), "out": out,
        })
    if not gens:
        return None
    lat = [g["lat"] for g in gens]
    inp = sum(g["inp"] for g in gens)
    out = sum(g["out"] for g in gens)
    model_time = sum(lat)
    tpots = [g["tpot"] for g in gens if g["tpot"] is not None]
    return {
        "llm_requests": len(gens),
        "llm_latency_avg_s": round(st.mean(lat), 3),
        "llm_latency_p95_s": round(sorted(lat)[max(int(0.95 * len(lat)) - 1, 0)], 3),
        "llm_ttft_avg_s": round(st.mean([g["ttft"] for g in gens]), 3),
        "llm_tpot_avg_s": round(st.mean(tpots), 4) if tpots else 0,
        "model_time_s": round(model_time, 3),
        "llm_tput_tokens_per_s": round(out / model_time, 3) if model_time else 0,
        "llm_tokens_input": inp, "llm_tokens_output": out,
        "llm_tokens_total": inp + out,
    }


class Api:
    def __init__(self, host, pk, sk):
        self.host = host.rstrip("/")
        self.s = requests.Session()
        self.s.auth = (pk, sk)


def find_trace(api: Api, task_name: str):
    """按 trace name（f'{task_name}#1'）找 trial trace，取最新一条。

    注意：列表接口（GET /traces）返回的 observations 只是 id 字符串数组，
    root span 与 scores 必须再查详情接口（GET /traces/{id}）。
    """
    want = f"{task_name}#1"
    r = api.s.get(f"{api.host}/api/public/traces",
                  params={"name": want, "limit": 10}, timeout=60).json()
    cands = [t for t in r.get("data", []) if isinstance(t, dict)]
    if not cands:
        return None
    cands.sort(key=lambda t: t["timestamp"], reverse=True)
    d = api.s.get(f"{api.host}/api/public/traces/{cands[0]['id']}",
                  timeout=60).json()
    obs = [o for o in d.get("observations", []) if isinstance(o, dict)]
    roots = [o for o in obs
             if o.get("parentObservationId") is None and o.get("name") == "trial"]
    root = roots[0] if roots else None
    return {"trace_id": d["id"], "scores": d.get("scores", []) or [], "root": root}


def score_id(trace_id: str, name: str) -> str:
    """确定性 score id：同 trace 同 name 幂等。

    Langfuse POST /api/public/scores 支持带 id 覆盖写（upsert）；
    注意 v4.26 的 DELETE /scores 只返回 202 排队且队列不保证消费，
    靠"先删后写"会积累重复行，故改用确定性 id。
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"langfuse-score:{trace_id}:{name}"))


def post_score(api: Api, trace_id, obs_id, name, value, dtype="NUMERIC"):
    r = api.s.post(f"{api.host}/api/public/scores", timeout=60, json={
        "id": score_id(trace_id, name),
        "traceId": trace_id, "observationId": obs_id,
        "name": name, "value": value, "dataType": dtype})
    return r.status_code in (200, 201), r.text[:120]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run-root", required=True,
                    help="harbor run 目录，如 jobs/<run_name>/<timestamp>")
    ap.add_argument("--host", default="http://localhost:3000")
    ap.add_argument("--pk", required=True, help="Langfuse public key（harbor-trial 项目）")
    ap.add_argument("--sk", required=True, help="Langfuse secret key")
    ap.add_argument("--dry-run", action="store_true", help="只计算打印，不上传")
    args = ap.parse_args()

    api = Api(args.host, args.pk, args.sk)
    run_root = Path(args.run_root)
    trials = discover_trials(run_root)
    if not trials:
        sys.exit(f"未在 {run_root} 下发现 trial（缺少 agent/trajectory.json）")
    print(f"发现 Trial：{len(trials)}")

    fail = 0
    for d in trials:
        task = trial_task_name(d)
        print(f"\n=== {d.name}  (task={task}) ===")
        traj_m = trajectory_metrics(d)
        for k in ("steps_total", "tool_calls_total", "tool.bash.calls",
                  "tool_errors_total", "tool_error_rate", "distinct_tools",
                  "tool.bash.interval_s", "redundant_call_ratio", "loop_detected",
                  "task_duration_s"):
            print(f"  {k:24s} = {traj_m[k]}")

        trace = find_trace(api, task)
        if trace is None or trace["root"] is None:
            print("  !! 未找到对应 Langfuse trace 或 root span，请先执行 atif2langfuse export")
            if not args.dry_run:
                fail += 1
            continue
        root = trace["root"]
        llm_m = llm_metrics(api, root["startTime"], root["endTime"])
        if llm_m is None:
            print("  !! 时间窗内未查到 litellm GENERATION，跳过 llm_* 与 model_time_s")
            if not args.dry_run:
                fail += 1
            continue

        merged = {**traj_m, **llm_m}
        merged["non_model_time_s"] = round(
            merged["task_duration_s"] - merged["model_time_s"], 3)

        print(f"  trace={trace['trace_id']}  root={root['id']}")
        print("  --- 待上传 score ---")
        for k in NUMERIC_SCORES + [LOOP_SCORE]:
            print(f"  {k:24s} = {merged[k]}")
        if args.dry_run:
            continue

        # 幂等由确定性 id 保证（v4.26 score DELETE 队列不消费，不做先删后写）
        total = len(NUMERIC_SCORES) + 1  # 20 NUMERIC + loop_detected
        ok = 0
        for k in NUMERIC_SCORES:
            good, msg = post_score(api, trace["trace_id"], root["id"], k, merged[k])
            ok += good
            if not good:
                print(f"  POST {k} 失败: {msg}")
        good, msg = post_score(api, trace["trace_id"], root["id"],
                               LOOP_SCORE, merged[LOOP_SCORE], "CATEGORICAL")
        ok += good
        if not good:
            print(f"  POST {LOOP_SCORE} 失败: {msg}")
        print(f"  上传完成：{ok}/{total} 个 score")
        if ok < total:
            fail += 1

    print(f"\n{'DRY-RUN ' if args.dry_run else ''}结束：{'全部成功' if fail == 0 else f'{fail} 个 trial 失败'}")
    sys.exit(0 if fail == 0 else 1)


if __name__ == "__main__":
    main()
