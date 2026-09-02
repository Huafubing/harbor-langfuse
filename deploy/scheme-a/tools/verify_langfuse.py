# -*- coding: utf-8 -*-
"""五指标验收脚本：查 Langfuse API 逐项断言，打印勾选清单。

检查项（每个 trial）：
  [1] trial trace 存在（tags=harbor + sessionId）
  [2] ① reward score
  [3] ② 轨迹质量 scores（tool_calls_total / redundant_call_ratio /
      tool_error_rate / loop_detected）
  [4] ③ task_duration_s + model_time_s + non_model_time_s
  [5] ④ proxy traces 已 join（sessionId 下非 harbor trace >=1）且
      gen_ai.server.time_to_first_token / time_per_output_token 可见
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
TTFT_KEY = "gen_ai.server.time_to_first_token"
TPOT_KEY = "gen_ai.server.time_per_output_token"


def _iter_gen_ai_values(obs: dict):
    """在 observation metadata（含嵌套 attributes）里找 gen_ai.server.* 值。"""
    md = obs.get("metadata")
    stacks = []
    if isinstance(md, dict):
        stacks.append(md)
        if isinstance(md.get("attributes"), dict):
            stacks.append(md["attributes"])
    for stack in stacks:
        for key, val in stack.items():
            if key in (TTFT_KEY, TPOT_KEY):
                yield key, val


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

        # ④ proxy join + TTFT/TPOT
        session_traces = [tr for tr in client.list_traces(sessionId=t.session_id)
                          if "harbor" not in (tr.get("tags") or [])]
        checks.append(("④ proxy traces joined", len(session_traces) >= 1,
                       f"{len(session_traces)} 条" if session_traces
                       else "无（跑 join_proxy_traces.py）"))

        ttft_seen = tpot_seen = False
        ttft_vals: list[float] = []
        for tr in session_traces:
            try:
                obs_list = client.get("/observations",
                                      params={"traceId": tr["id"],
                                              "limit": 50}).get("data") or []
            except Exception:  # noqa: BLE001
                obs_list = []
            for obs in obs_list:
                for key, val in _iter_gen_ai_values(obs):
                    if key == TTFT_KEY:
                        ttft_seen = True
                        try:
                            ttft_vals.append(float(val))
                        except (TypeError, ValueError):
                            pass
                    if key == TPOT_KEY:
                        tpot_seen = True
        checks.append(("④ TTFT 属性可见", ttft_seen,
                       f"值={ttft_vals}" if ttft_seen
                       else "未见（非流式请求 TTFT=0，或属性在 metadata 他处）"))
        checks.append(("④ TPOT 属性可见", tpot_seen,
                       "ok" if tpot_seen else "未见"))

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
