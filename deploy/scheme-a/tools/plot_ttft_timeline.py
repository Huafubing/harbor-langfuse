# -*- coding: utf-8 -*-
"""Trial 内多步 LLM 请求的 TTFT 时间线柱状图（单文件 HTML，零依赖）。

背景：LiteLLM proxy 的流式请求带原生 timeToFirstToken（秒，真值），
每步一个 litellm-acompletion GENERATION。Langfuse dashboard 的 widget
是聚合型（observations 视图无细粒度时间维度，仅 startTimeMonth），
做不了"每步一根柱"的时间线，因此本脚本按 trial 出本地图。

x 轴 = 请求序号（按 startTime 排序，等间距；间隔不均匀，真时间轴会把
柱挤在一起）。柱 hover 可见绝对时刻 / TTFT / latency / 输出 token。
TTFT >= p95 的柱标橙色（定位慢启动请求）；缺失 TTFT（非流式）为灰色。

数据口径与 join_proxy_traces.py 一致：result.json 时间窗内的
GENERATION，过滤 harbor tag 即 proxy 请求。默认 --window 0
（三个 trial 背靠背串行时，外扩窗口会卷入相邻 trial 的请求）。

用法：cd scheme-a && set -a && source .env && set +a
      python3 tools/plot_ttft_timeline.py --run-root <runs目录> [--window 0]
输出：<run-root>/ttft_timeline.html（浏览器直接打开）
"""
from __future__ import annotations

import argparse
import html
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from join_proxy_traces import _trial_window  # noqa: E402
from lf_client import LangfuseClient  # noqa: E402

C_NORMAL = "#4e79a7"   # 常规柱
C_SLOW = "#f28e2b"     # TTFT >= p95
C_MISS = "#c9c9c9"     # TTFT 缺失（非流式）
SVG_W, SVG_H = 1160, 280
M_L, M_R, M_T, M_B = 46, 12, 14, 36


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def _p95(sorted_vals: list[float]) -> float:
    return sorted_vals[min(len(sorted_vals) - 1,
                           round(0.95 * (len(sorted_vals) - 1)))]


def fetch_trial(client: LangfuseClient, trial, window_s: float) -> list[dict]:
    """窗口内 proxy GENERATION（无 harbor tag），按 startTime 排序。"""
    window = _trial_window(trial)
    if window is None:
        return []
    from datetime import timedelta
    t0, t1 = window
    gens = client.list_generations(
        from_ts=(t0 - timedelta(seconds=window_s)).isoformat(),
        to_ts=(t1 + timedelta(seconds=window_s)).isoformat())
    rows = []
    for g in gens:
        if "harbor" in (g.get("tags") or []):
            continue
        start = _parse(g.get("startTime"))
        if start is None:
            continue
        rows.append({"start": start,
                     "ttft": (g.get("timeToFirstToken")
                              if isinstance(g.get("timeToFirstToken"),
                                            (int, float)) else None),
                     "latency": g.get("latency"),
                     "out": g.get("outputTokens")})
    rows.sort(key=lambda r: r["start"])
    return rows


def svg_bars(rows: list[dict]) -> str:
    """单 trial 的 TTFT 柱状图（SVG）。rows 已按时间排序。"""
    plot_w = SVG_W - M_L - M_R
    plot_h = SVG_H - M_T - M_B
    vals = [r["ttft"] for r in rows if r["ttft"] is not None]
    y_max = (max(vals) * 1.08) if vals else 1.0
    if y_max <= 0:
        y_max = 1.0
    p95 = _p95(sorted(vals)) if vals else None

    out = [f'<svg viewBox="0 0 {SVG_W} {SVG_H}" width="100%" '
           f'role="img" xmlns="http://www.w3.org/2000/svg">']
    # y 网格与刻度（0 ~ y_max 五档）
    for i in range(5):
        v = y_max * i / 4
        y = M_T + plot_h - plot_h * i / 4
        out.append(f'<line x1="{M_L}" y1="{y:.1f}" x2="{SVG_W - M_R}" '
                   f'y2="{y:.1f}" stroke="#e5e7eb" stroke-width="1"/>')
        out.append(f'<text x="{M_L - 6}" y="{y + 4:.1f}" font-size="11" '
                   f'fill="#6b7280" text-anchor="end">{v:.1f}</text>')
    out.append(f'<text x="{M_L - 6}" y="{M_T - 3}" font-size="11" '
               f'fill="#6b7280" text-anchor="end">TTFT(s)</text>')

    n = len(rows)
    step = plot_w / n if n else plot_w
    bar_w = max(step - 1.0, 0.8)
    t_first, t_last = rows[0]["start"], rows[-1]["start"]
    loc = t_first.astimezone()
    for i, r in enumerate(rows):
        x = M_L + i * step
        if r["ttft"] is None:
            h, color, miss = plot_h, C_MISS, "TTFT 缺失（非流式）"
        else:
            h = plot_h * r["ttft"] / y_max
            color = C_SLOW if (p95 is not None and r["ttft"] >= p95) else C_NORMAL
            miss = ""
        y = M_T + plot_h - h
        tt = (f"step {i + 1}/{n} · {r['start'].astimezone():%H:%M:%S}"
              f"&#10;TTFT: {'—' if r['ttft'] is None else f'{r['ttft']:.2f}s'}{miss}"
              f"&#10;latency: {r['latency'] if r['latency'] is not None else '—'}s"
              f"&#10;output: {r['out'] if r['out'] is not None else '—'} tok")
        out.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" '
                   f'height="{h:.2f}" fill="{color}">'
                   f'<title>{tt}</title></rect>')
    # x 轴基线与首末时刻（本地时区）
    out.append(f'<line x1="{M_L}" y1="{M_T + plot_h}" '
               f'x2="{SVG_W - M_R}" y2="{M_T + plot_h}" '
               f'stroke="#9ca3af" stroke-width="1"/>')
    out.append(f'<text x="{M_L}" y="{SVG_H - 8}" font-size="11" '
               f'fill="#6b7280">{loc:%H:%M:%S}（step 1）</text>')
    out.append(f'<text x="{SVG_W - M_R}" y="{SVG_H - 8}" font-size="11" '
               f'fill="#6b7280" text-anchor="end">'
               f'{t_last.astimezone():%H:%M:%S}（step {n}）</text>')
    if p95 is not None:
        out.append(f'<text x="{SVG_W - M_R}" y="{M_T + 4}" font-size="11" '
                   f'fill="#b45309" text-anchor="end">'
                   f'橙 = TTFT ≥ p95（{p95:.2f}s）</text>')
    out.append("</svg>")
    return "\n".join(out)


def render(trials: list[tuple[str, list[dict]]], run_root: str,
           window_s: float) -> str:
    parts = ["""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>TTFT 时间线（per-step）</title><style>
body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",
sans-serif;margin:24px;color:#111827;background:#fff}
h1{font-size:20px} h2{font-size:15px;margin:28px 0 6px}
table{border-collapse:collapse;font-size:13px;margin:10px 0}
td,th{border:1px solid #d1d5db;padding:4px 10px;text-align:right}
th{background:#f3f4f6} td:first-child,th:first-child{text-align:left}
.note{color:#6b7280;font-size:12px;margin:6px 0}
.meta{color:#374151;font-size:13px}
</style></head><body>"""]
    parts.append("<h1>Trial 内多步 LLM 请求 TTFT 时间线</h1>")
    parts.append(f'<p class="meta">run：<code>{html.escape(run_root)}</code>'
                 f" · 数据：Langfuse proxy GENERATION（原生 timeToFirstToken"
                 f" 流式真值） · 时间窗 = result.json ± {window_s:.0f}s"
                 f"</p>")
    parts.append('<p class="note">x 轴为请求序号（按发起时刻排序，等间距，'
                 "非真实时间比例）；柱内 hover 查看绝对时刻与明细；"
                 "灰柱 = 非流式（TTFT 缺失）。</p>")

    parts.append("<table><tr><th>Trial</th><th>请求数</th><th>TTFT avg</th>"
                 "<th>p95</th><th>max</th><th>首步</th></tr>")
    for name, rows in trials:
        vals = [r["ttft"] for r in rows if r["ttft"] is not None]
        if vals:
            parts.append(
                f"<tr><td>{html.escape(name)}</td><td>{len(rows)}</td>"
                f"<td>{sum(vals) / len(vals):.3f}s</td>"
                f"<td>{_p95(sorted(vals)):.3f}s</td>"
                f"<td>{max(vals):.3f}s</td><td>{vals[0]:.3f}s</td></tr>")
        else:
            parts.append(f"<tr><td>{html.escape(name)}</td>"
                         f"<td>{len(rows)}</td><td colspan='4'>无 TTFT</td></tr>")
    parts.append("</table>")

    for name, rows in trials:
        vals = [r["ttft"] for r in rows if r["ttft"] is not None]
        avg = f"{sum(vals) / len(vals):.3f}s" if vals else "—"
        parts.append(f"<h2>{html.escape(name)}"
                     f" <span class='note'>({len(rows)} 请求 · avg {avg})</span>"
                     f"</h2>")
        parts.append(svg_bars(rows) if rows else "<p class='note'>无请求</p>")
    parts.append("</body></html>")
    return "\n".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Trial 内多步 TTFT 时间线柱状图")
    ap.add_argument("--run-root", required=True)
    ap.add_argument("--window", type=float, default=0.0,
                    help="时间窗外扩秒数（默认 0；背靠背 trial 外扩会串窗）")
    ap.add_argument("--out", default=None,
                    help="输出 HTML 路径（默认 <run-root>/ttft_timeline.html）")
    args = ap.parse_args(argv)

    from atif2langfuse.reader import load_trials
    trials_data, _ = load_trials(args.run_root)
    if not trials_data:
        print("未发现 Trial", file=sys.stderr)
        return 1

    client = LangfuseClient()
    collected: list[tuple[str, list[dict]]] = []
    for t in trials_data:
        rows = fetch_trial(client, t, args.window)
        collected.append((t.trial_dir.name, rows))
        vals = [r["ttft"] for r in rows if r["ttft"] is not None]
        avg = f"{sum(vals) / len(vals):.3f}s" if vals else "—"
        print(f"  [{t.trial_dir.name}] requests={len(rows)} ttft_avg={avg}")

    out = Path(args.out) if args.out else \
        Path(args.run_root) / "ttft_timeline.html"
    out.write_text(render(collected, args.run_root, args.window),
                   encoding="utf-8")
    print(f"已生成：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
