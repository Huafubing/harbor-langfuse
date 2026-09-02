# -*- coding: utf-8 -*-
"""Proxy trace 关联 + 三段拆解。

把 LiteLLM proxy（OTel callback）产生的模型请求 trace，按时间窗归入对应
trial 的 session，并计算三段拆解 scores：

  model_time_s      模型段真值（该窗口内 proxy trace 的 latency 总和）
  non_model_time_s  工具+overhead 合计（task_duration - model_time，残差口径）
  task_duration_s   端到端真值（轨迹首末时间戳，与 analyzer 同源）

时间窗来源：result.json 的 started_at/finished_at（缺失时回退轨迹首末）。
验证期单并发下为精确匹配；并发场景为时间分桶近似（报告需标注口径）。

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

        # 窗口内全部 trace；proxy trace = 不带 harbor tag 的
        try:
            candidates = client.list_traces(
                fromTimestamp=from_ts, toTimestamp=to_ts)
        except Exception as exc:  # noqa: BLE001
            print(f"  [{t.trial_dir.name}] 查询失败：{exc}", file=sys.stderr)
            continue
        proxy = []
        for tr in candidates:
            tags = tr.get("tags") or []
            if "harbor" in tags:
                continue
            ts = _parse_ts(tr.get("timestamp"))
            if ts and t0 - timedelta(seconds=args.window) <= ts \
                    <= t1 + timedelta(seconds=args.window):
                proxy.append(tr)

        # 定位 trial trace（挂三段 scores 的目标）
        trial_trace = client.find_trial_trace(t.session_id)
        trace_id = trial_trace["id"] if trial_trace else None

        # PATCH proxy traces → 同 session + 标记 tag
        joined = 0
        for tr in proxy:
            new_tags = sorted(set((tr.get("tags") or []) + ["proxy", t.trial_dir.name]))
            try:
                client.patch_trace(tr["id"], {"sessionId": t.session_id,
                                              "tags": new_tags})
                joined += 1
            except Exception as exc:  # noqa: BLE001
                print(f"    [patch:fail] {tr['id']}: {exc}", file=sys.stderr)

        # 三段拆解
        model_ms = 0.0
        for tr in proxy:
            ms = tr.get("latencyMs")
            if isinstance(ms, (int, float)):
                model_ms += ms
        task_s = None
        if trial_trace and trial_trace.get("latencyMs") is not None:
            task_s = trial_trace["latencyMs"] / 1000.0
        else:
            from analyzer import analyze_trial
            m = analyze_trial(t)
            task_s = m.get("task_duration_s")

        model_s = round(model_ms / 1000.0, 3)
        scores = []
        if trace_id and task_s is not None:
            non_model = round(max(task_s - model_s, 0.0), 3)
            scores = [
                {"traceId": trace_id, "name": "model_time_s", "value": model_s,
                 "dataType": "NUMERIC",
                 "comment": "proxy 真值：窗口内模型请求 latency 总和",
                 "metadata": {"source": "scheme-a-join",
                              "proxy_traces": len(proxy)}},
                {"traceId": trace_id, "name": "non_model_time_s",
                 "value": non_model, "dataType": "NUMERIC",
                 "comment": "残差口径：task_duration - model_time（工具+overhead 合计）",
                 "metadata": {"source": "scheme-a-join"}},
            ]
            for s in scores:
                try:
                    client.post_score(s)
                    total_scores += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"    [score:fail] {s['name']}: {exc}", file=sys.stderr)

        total_joined += joined
        print(f"  [{t.trial_dir.name}] proxy traces joined={joined} "
              f"model_time={model_s}s task={task_s}s "
              f"trace={'found' if trace_id else 'MISSING(run export first)'}")

    print(f"完成：joined={total_joined} 段拆解 scores={total_scores}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
