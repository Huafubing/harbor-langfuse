# -*- coding: utf-8 -*-
"""离线轨迹分析器：从 ATIF trajectory.json 计算 L2 轨迹质量指标（零埋点真值）。

产出（挂到 trial trace 的 Scores）：
  tool_calls_total        工具调用总次数
  distinct_tools          不同工具数
  redundant_call_ratio    冗余调用比例（同签名重复出现）
  tool_error_rate         observation 命中错误模式的比例（启发式，口径透明）
  tool_errors_total       命中错误模式的调用绝对数
  steps_total             agent step 总数
  loop_detected           死循环检测（连续>=3 次相同调用+相同结果） categorical
  task_duration_s         端到端时长（轨迹首末时间戳，真值）
  tool.<name>.interval_s  per-tool 平均 step 间隔（近似口径：含模型时间）
  tool.<name>.calls       per-tool 调用次数

幂等：挂载前查 trace 已有 score 名，同名跳过（管线重跑不产生重复）。

用法：
  python3 analyzer.py --run-root <runs目录> [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from atif2langfuse.reader import load_trials  # noqa: E402

ERROR_PATTERNS = (
    "error", "failed", "exception", "traceback", "command not found",
    "no such file", "permission denied", "timed out", "timeout",
)
LOOP_REPEATS = 3


def _norm_args(args) -> str:
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False)
    except TypeError:
        return str(args)


def _obs_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8]


def _parse_ts(ts):
    if not ts:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def analyze_trial(trial) -> dict:
    steps = trial.steps
    calls = []          # (fn, norm_args, obs_text, step_idx, ts, next_ts)
    for i, s in enumerate(steps):
        if not s.tool_calls:
            continue
        next_ts = steps[i + 1].timestamp if i + 1 < len(steps) else None
        for tc in s.tool_calls:
            calls.append((tc.get("function_name") or "unknown",
                          _norm_args(tc.get("arguments")),
                          s.observation_text or "", i, s.timestamp, next_ts))

    total = len(calls)
    distinct = len({c[0] for c in calls})

    # 冗余：同一签名（fn+args）出现次数 > 1 的额外次数
    sig_counts: dict[str, int] = {}
    for c in calls:
        sig_counts[f"{c[0]}|{c[1]}"] = sig_counts.get(f"{c[0]}|{c[1]}", 0) + 1
    redundant_extra = sum(n - 1 for n in sig_counts.values() if n > 1)
    redundant_ratio = (redundant_extra / total) if total else 0.0

    # 错误率：observation 命中错误模式
    err = sum(1 for c in calls
              if any(p in c[2].lower() for p in ERROR_PATTERNS))
    error_rate = (err / total) if total else 0.0

    # 死循环：连续 >= LOOP_REPEATS 次相同 (fn, args, obs_hash)
    loop = False
    if calls:
        run_len, prev_sig = 1, (calls[0][0], calls[0][1], _obs_hash(calls[0][2]))
        for c in calls[1:]:
            sig = (c[0], c[1], _obs_hash(c[2]))
            run_len = run_len + 1 if sig == prev_sig else 1
            prev_sig = sig
            if run_len >= LOOP_REPEATS:
                loop = True
                break

    # 端到端时长（真值）
    t0 = _parse_ts(next((s.timestamp for s in steps if s.timestamp), None))
    t1 = _parse_ts(next((s.timestamp for s in reversed(steps) if s.timestamp), None))
    duration = (t1 - t0).total_seconds() if t0 and t1 else None

    # per-tool 平均 step 间隔（近似：含模型时间）与调用次数
    per_tool: dict[str, list[float]] = {}
    per_tool_calls: dict[str, int] = {}
    for fn, _args, _obs, _i, ts, next_ts in calls:
        per_tool_calls[fn] = per_tool_calls.get(fn, 0) + 1
        a, b = _parse_ts(ts), _parse_ts(next_ts)
        if a and b and (b - a).total_seconds() >= 0:
            per_tool.setdefault(fn, []).append((b - a).total_seconds())

    return {
        "tool_calls_total": total,
        "distinct_tools": distinct,
        "redundant_call_ratio": round(redundant_ratio, 4),
        "tool_error_rate": round(error_rate, 4),
        "tool_errors_total": err,
        "steps_total": len(steps),
        "loop_detected": "loop" if loop else "none",
        "task_duration_s": round(duration, 3) if duration is not None else None,
        "per_tool_interval": {
            fn: round(sum(v) / len(v), 3) for fn, v in per_tool.items()
        },
        "per_tool_calls": per_tool_calls,
    }


NUMERIC = ("tool_calls_total", "distinct_tools", "redundant_call_ratio",
           "tool_error_rate", "tool_errors_total", "steps_total",
           "task_duration_s")


def build_scores(metrics: dict, trace_id: str | None) -> list[dict]:
    items: list[dict] = []
    for key in NUMERIC:
        v = metrics.get(key)
        if v is None:
            continue
        items.append({"traceId": trace_id, "name": key, "value": v,
                      "dataType": "NUMERIC",
                      "metadata": {"source": "scheme-a-analyzer"}})
    items.append({"traceId": trace_id, "name": "loop_detected",
                  "value": metrics["loop_detected"],
                  "dataType": "CATEGORICAL",
                  "metadata": {"source": "scheme-a-analyzer"}})
    for fn, mean_s in metrics.get("per_tool_interval", {}).items():
        items.append({"traceId": trace_id,
                      "name": f"tool.{fn}.interval_s",
                      "value": mean_s, "dataType": "NUMERIC",
                      "comment": "近似口径：step 间隔，含模型时间",
                      "metadata": {"source": "scheme-a-analyzer"}})
    for fn, n in metrics.get("per_tool_calls", {}).items():
        items.append({"traceId": trace_id,
                      "name": f"tool.{fn}.calls",
                      "value": n, "dataType": "NUMERIC",
                      "metadata": {"source": "scheme-a-analyzer"}})
    return items


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ATIF 轨迹质量离线分析 → Langfuse Scores")
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="只计算打印，不上报（无需 Langfuse）")
    args = ap.parse_args(argv)

    trials, skipped = load_trials(args.run_root)
    print(f"发现 Trial：{len(trials)}（跳过 {skipped}）")
    if not trials:
        return 0

    client = None
    if not args.dry_run:
        from lf_client import LangfuseClient
        client = LangfuseClient()

    ok = fail = 0
    for t in trials:
        metrics = analyze_trial(t)
        print(f"  [{t.trial_dir.name}]")
        for k in NUMERIC:
            print(f"    {k:24s} = {metrics[k]}")
        print(f"    {'loop_detected':24s} = {metrics['loop_detected']}")
        for fn, v in metrics["per_tool_interval"].items():
            print(f"    tool.{fn}.interval_s      = {v}s (近似)")

        if args.dry_run:
            continue
        trace = client.find_trial_trace(t.session_id)
        if trace is None:
            print(f"    [skip] 未找到 trial trace（先运行 atif2langfuse export）",
                  file=sys.stderr)
            fail += 1
            continue
        existing: set[str] = set()
        try:
            existing = {s["name"] for s in client.trace_scores(trace["id"])}
        except Exception:  # noqa: BLE001
            existing = set()
        for item in build_scores(metrics, trace["id"]):
            if item["name"] in existing:
                continue
            try:
                client.post_score(item)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                fail += 1
                print(f"    [score:fail] {item['name']}: {exc}", file=sys.stderr)
        print(f"    -> scores 挂载 trace {trace['id']}"
              f"（幂等跳过 {len(existing)} 个已有）")

    if args.dry_run:
        print("dry-run 完成（未联网）")
    else:
        print(f"完成：scores ok={ok} fail={fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
