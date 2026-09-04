# -*- coding: utf-8 -*-
"""五指标验收脚本：查 Langfuse API 逐项断言，打印勾选清单。

检查项（每个 trial）：
  [1] trial trace 存在（tags=harbor + sessionId）
  [2] ① reward score
  [3] ② 轨迹质量 scores（tool_calls_total / redundant_call_ratio /
      tool_error_rate / loop_detected）
  [4] ③ task_duration_s + model_time_s + non_model_time_s
  [5] ④ proxy traces 已 join（session 下非 trial trace >=1），
      原生 timeToFirstToken 可见，TPOT 可按 (latency - ttft)/max(output-1,1)
      计算（ttft 缺失时退化为 latency/max(output-1,1)，即非流式口径）
  [6] ⑤ per-tool interval scores（近似口径）

用法：
  python3 verify_langfuse.py --run-root <runs目录>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from atif2langfuse.reader import load_trials  # noqa: E402
from lf_client import LangfuseClient  # noqa: E402

REQUIRED_QUALITY = ("tool_calls_total", "redundant_call_ratio",
                    "tool_error_rate", "loop_detected")
REQUIRED_PHASES = ("task_duration_s", "model_time_s", "non_model_time_s")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="五指标 Langfuse 验收清单")
    ap.add_argument("--run-root", required=True)
    args = ap.parse_args(argv)

    trials, _ = load_trials(args.run_root)
    print(f"发现 Trial：{len(trials)}")
    client = LangfuseClient()

    all_ok = True
    for t in trials:
        name = t.trial_dir.name
        checks: list[tuple[str, bool, str]] = []

        trace = client.find_trial_trace(t.session_id)
        checks.append(("trial trace 存在", trace is not None,
                       trace["id"] if trace else "未找到（先 atif2langfuse export）"))
        if trace is None:
            all_ok = False
            print(f"  [{name}]")
            for label, ok, note in checks:
                print(f"    {'PASS' if ok else 'FAIL'}  {label}  {note}")
            continue

        scores = {s["name"]: s for s in client.trace_scores(trace["id"])}

        # ① reward
        reward_names = [n for n in scores if n == "reward" or n.startswith("reward.")]
        checks.append(("① reward score", bool(reward_names),
                       ",".join(reward_names) or "无（verifier 判分未回填？）"))

        # ② 轨迹质量
        missing_q = [k for k in REQUIRED_QUALITY if k not in scores]
        checks.append(("② 轨迹质量 scores", not missing_q,
                       "缺：" + ",".join(missing_q) if missing_q else "4/4 齐"))

        # ③ 三段
        missing_p = [k for k in REQUIRED_PHASES if k not in scores]
        checks.append(("③ 三段拆解 scores", not missing_p,
                       "缺：" + ",".join(missing_p) if missing_p else
                       f"task={scores.get('task_duration_s', {}).get('value')}"
                       f"/model={scores.get('model_time_s', {}).get('value')}"
                       f"/non_model={scores.get('non_model_time_s', {}).get('value')}"))

        # ④ proxy join + TTFT/TPOT（原生 langfuse callback 字段）
        # v2 observations 的 sessionId 过滤命中 events 表固化列（ingest 时
        # 写入），看不到 join 的 PATCH；v1 GET /traces 读 v3 traces 表，
        # PATCH 结果立即可见。
        session_traces = [tr for tr in client.list_traces_v1(t.session_id)
                          if tr.get("id") != trace["id"]]
        checks.append(("④ proxy traces joined", len(session_traces) >= 1,
                       f"{len(session_traces)} 条" if session_traces
                       else "无（跑 join_proxy_traces.py）"))

        ttft_vals: list[float] = []
        tpot_vals: list[float] = []
        for tr in session_traces:
            for g in client.list_generations(trace_id=tr["id"]):
                ttft = g.get("timeToFirstToken")
                lat = g.get("latency")
                out = g.get("outputTokens")
                if isinstance(ttft, (int, float)):
                    ttft_vals.append(round(ttft, 3))
                if isinstance(lat, (int, float)) \
                        and isinstance(out, (int, float)) and out >= 1:
                    base = ttft if isinstance(ttft, (int, float)) else 0.0
                    tpot_vals.append(round(
                        (lat - base) / max(out - 1, 1), 4))
        checks.append(("④ TTFT 可见（原生 timeToFirstToken）", bool(ttft_vals),
                       f"值={ttft_vals}" if ttft_vals
                       else "未见（非流式请求该值为 null）"))
        checks.append(("④ TPOT 可计算", bool(tpot_vals),
                       f"值={tpot_vals}" if tpot_vals
                       else "无法计算（缺 latency/usage，或 join 未跑）"))

        # ⑤ per-tool
        tool_scores = [n for n in scores if n.startswith("tool.") and
                       n.endswith(".interval_s")]
        checks.append(("⑤ per-tool 时延 scores", bool(tool_scores),
                       ",".join(tool_scores) or "无（跑 analyzer.py）"))

        ok_all = all(ok for _, ok, _ in checks)
        all_ok &= ok_all
        print(f"  [{name}] {'ALL PASS' if ok_all else 'HAS FAIL'}")
        for label, ok, note in checks:
            print(f"    {'PASS' if ok else 'FAIL'}  {label}  {note}")

    print("=" * 60)
    print("验收结论：", "全部通过" if all_ok else "存在未通过项（见上）")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
