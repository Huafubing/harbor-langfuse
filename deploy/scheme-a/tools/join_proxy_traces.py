# -*- coding: utf-8 -*-
"""Proxy trace 关联 + 三段拆解。

把 LiteLLM proxy（原生 langfuse callback）产生的模型请求 GENERATION，
按时间窗归入对应 trial 的 session，并计算三段拆解 scores：

  model_time_s      模型段真值（该窗口内 GENERATION 的 latency（秒）总和）
  non_model_time_s  工具+overhead 合计（task_duration - model_time，残差口径）
  task_duration_s   端到端真值（轨迹首末时间戳，与 analyzer 同源）

时间窗来源：result.json 的 started_at/finished_at（缺失时回退轨迹首末）。
验证期单并发下为精确匹配；并发场景为时间分桶近似（报告需标注口径）。

注：PATCH sessionId 只更新 v3 traces 表；v2 observations 的 sessionId
过滤命中 events 表固化列，因此本脚本用时间窗找候选，而不是 sessionId。

用法：
  python3 join_proxy_traces.py --run-root <runs目录> [--window 120]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from atif2langfuse.reader import load_trials  # noqa: E402
from lf_client import LangfuseClient  # noqa: E402


def _parse_ts(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _trial_window(trial) -> tuple[datetime, datetime] | None:
    """优先 result.json 的 started_at/finished_at，回退轨迹首末时间戳。"""
    result = {}
    rp = trial.trial_dir / "result.json"
    if rp.exists():
        try:
            import json
            result = json.loads(rp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            result = {}
    t0 = _parse_ts(result.get("started_at")) or _parse_ts(
        next((s.timestamp for s in trial.steps if s.timestamp), None))
    t1 = _parse_ts(result.get("finished_at")) or _parse_ts(
        next((s.timestamp for s in reversed(trial.steps) if s.timestamp), None))
    if t0 and t1:
        return t0, t1
    return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(tz=None).astimezone().isoformat()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="proxy trace 时间窗 join + 三段拆解")
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--window", type=float, default=120.0,
                    help="时间窗外扩秒数（默认 120，覆盖 job 启动偏移）")
    args = ap.parse_args(argv)

    trials, _ = load_trials(args.run_root)
    print(f"发现 Trial：{len(trials)}")
    if not trials:
        return 0

    client = LangfuseClient()
    total_joined = total_scores = 0

    for t in trials:
        window = _trial_window(t)
        if window is None:
            print(f"  [{t.trial_dir.name}] 无时间信息，跳过", file=sys.stderr)
            continue
        t0, t1 = window
        from_ts = (t0 - timedelta(seconds=args.window)).isoformat()
        to_ts = (t1 + timedelta(seconds=args.window)).isoformat()

        # 窗口内全部 GENERATION；proxy 的 = 不带 harbor tag 的
        #（tags 为 trace 级，取自 trace_context 组）
        try:
            candidates = client.list_generations(
                from_ts=from_ts, to_ts=to_ts)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{t.trial_dir.name}] 查询失败：{exc}", file=sys.stderr)
            continue
        proxy = [g for g in candidates
                 if "harbor" not in (g.get("tags") or [])]

        # 定位 trial trace（挂三段 scores 的目标）
        trial_trace = client.find_trial_trace(t.session_id)
        trace_id = trial_trace["id"] if trial_trace else None

        # PATCH proxy traces → 同 session + 标记 tag（同 trace 去重）
        joined = 0
        patched: set[str] = set()
        for g in proxy:
            tid = g["traceId"]
            if not tid or tid in patched:
                continue
            new_tags = sorted(set((g.get("tags") or [])
                                  + ["proxy", t.trial_dir.name]))
            try:
                client.patch_trace(tid, {"sessionId": t.session_id,
                                         "tags": new_tags})
                patched.add(tid)
                joined += 1
            except Exception as exc:  # noqa: BLE001
                print(f"    [patch:fail] {tid}: {exc}", file=sys.stderr)

        # 三段拆解（GENERATION latency 单位为秒）
        model_s = round(sum(g["latency"] for g in proxy
                            if isinstance(g.get("latency"), (int, float))), 3)
        from analyzer import analyze_trial
        task_s = analyze_trial(t).get("task_duration_s")

        # LLM 请求聚合（proxy 口径，供时延面板使用）
        lats = sorted(g["latency"] for g in proxy
                      if isinstance(g.get("latency"), (int, float)))
        ttfts = [g["timeToFirstToken"] for g in proxy
                 if isinstance(g.get("timeToFirstToken"), (int, float))]
        tpots = [(g["latency"] - (g["timeToFirstToken"]
                  if isinstance(g.get("timeToFirstToken"), (int, float))
                  else 0.0)) / max(g["outputTokens"] - 1, 1)
                 for g in proxy
                 if isinstance(g.get("latency"), (int, float))
                 and isinstance(g.get("outputTokens"), (int, float))
                 and g.get("outputTokens") >= 1]
        tin = sum(g["inputTokens"] for g in proxy
                  if isinstance(g.get("inputTokens"), (int, float)))
        tout = sum(g["outputTokens"] for g in proxy
                   if isinstance(g.get("outputTokens"), (int, float)))
        p95 = lats[min(len(lats) - 1, round(0.95 * (len(lats) - 1)))] if lats else 0.0
        agg = {
            "llm_requests": float(len(lats)),
            "llm_tokens_input": float(tin),
            "llm_tokens_output": float(tout),
            "llm_tokens_total": float(tin + tout),
            "llm_latency_avg_s": round(sum(lats) / len(lats), 3) if lats else 0.0,
            "llm_latency_p95_s": round(p95, 3),
            "llm_ttft_avg_s": (round(sum(ttfts) / len(ttfts), 3)
                               if ttfts else None),
            "llm_tpot_avg_s": (round(sum(tpots) / len(tpots), 4)
                               if tpots else None),
            "llm_tput_tokens_per_s": (round(tout / sum(lats), 3)
                                      if lats and sum(lats) > 0 else None),
        }

        scores = []
        if trace_id and task_s is not None:
            non_model = round(max(task_s - model_s, 0.0), 3)
            scores = [
                {"traceId": trace_id, "name": "model_time_s", "value": model_s,
                 "dataType": "NUMERIC",
                 "comment": "proxy 真值：窗口内 GENERATION latency（秒）总和",
                 "metadata": {"source": "scheme-a-join",
                              "proxy_generations": len(proxy)}},
                {"traceId": trace_id, "name": "non_model_time_s",
                 "value": non_model, "dataType": "NUMERIC",
                 "comment": "残差口径：task_duration - model_time（工具+overhead 合计）",
                 "metadata": {"source": "scheme-a-join"}},
            ]
        for name, v in agg.items():
            if v is None or not trace_id:
                continue
            scores.append({
                "traceId": trace_id, "name": name, "value": v,
                "dataType": "NUMERIC",
                "comment": "proxy 聚合真值（窗口内 GENERATION 统计）"
                           + ("；流式请求原生 timeToFirstToken 均值（真值）"
                              if name == "llm_ttft_avg_s" else
                              "；(latency−TTFT)/max(output−1,1) 均值，"
                              "TTFT 缺失时退化为 latency/max(output−1,1)"
                              if name == "llm_tpot_avg_s" else ""),
                "metadata": {"source": "scheme-a-join"}})

        # 幂等：trace 上已存在的同名 score 跳过（管线重跑不产生重复）
        existing: set[str] = set()
        if trace_id:
            try:
                existing = {s["name"] for s in client.trace_scores(trace_id)}
            except Exception:  # noqa: BLE001
                existing = set()
        for s in scores:
            if s["name"] in existing:
                continue
            try:
                client.post_score(s)
                total_scores += 1
            except Exception as exc:  # noqa: BLE001
                print(f"    [score:fail] {s['name']}: {exc}", file=sys.stderr)

        total_joined += joined
        print(f"  [{t.trial_dir.name}] generations={len(proxy)} "
              f"traces joined={joined} model_time={model_s}s task={task_s}s "
              f"trace={'found' if trace_id else 'MISSING(run export first)'}")

    print(f"完成：joined={total_joined} 段拆解 scores={total_scores}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
