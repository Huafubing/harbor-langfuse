# -*- coding: utf-8 -*-
"""按类别创建 Langfuse Dashboard：总体信息 + 局部（按 Trial）信息分层视图。

三个面板（12 列网格；分层：总体卡片 → 构成/分布 → 按 Trial 明细，
同一指标只在一个视图出现，不重复）：
  1. Harbor · 评测结果
     ① 总体卡片：Trial 总数 / 平均 reward / reward 中位数 / 最差 reward
     ② 总体分布：reward 直方图 + 通过构成饼图
     ③ 局部按 Trial：各 Trial reward 柱状 + Trial 明细透视表
  2. Harbor · 轨迹质量
     ① 总体卡片：工具调用数 / 工具错误率 / 冗余调用率 / 步骤数（均值）
     ② 局部按 Trial：工具错误率横条 + 工具调用数柱状（定位异常 case）
     ③ 明细：Trial×指标 全宽透视表（含绝对数 errors/steps）
  3. Harbor · 时延性能
     ① 总体卡片：任务时长 / 模型段 / 非模型段 / LLM 请求时延（均值）
     ② 时间构成：模型段 vs 非模型段 饼图（sum 占比）+ Trial×阶段 透视表
     ③ LLM 请求：时延 avg/p95/TTFT/TPOT + 各 Trial 生成吞吐 + Token（in/out/合计）
     ④ 请求量：各 Trial LLM 请求数；工具：per-tool 时延

图表选型原则：饼图只用于"构成占比"（通过/未通过、模型/非模型段）；
数量与速率对比（调用数、请求数、时延、token、吞吐、错误率）用柱状/横条；
比率类单指标无整体-部分关系，不做饼图。distinct_tools 当前恒为 1
（单工具 bash），不上面板。

实现走 unstable dashboards public API（Langfuse v4）：
  POST /api/public/unstable/dashboards
  POST /api/public/unstable/dashboard-widgets
  POST /api/public/unstable/dashboards/{id}/placements   （x/y/width/height）
幂等：同名 dashboard 已存在则跳过；--force 先删后建（级联删 widget）。

数据口径：
  - scores 全部由 analyzer / join_proxy_traces 产出（proxy GENERATION 聚合，
    单位秒）；不用 observations 视图 —— trial trace 内 ATIF 导出的 GENERATION
    与 proxy trace 的 GENERATION 重复且单位不一，混查会双算。
  - TTFT/TPOT 为流式真值（原生 timeToFirstToken，经 proxy 口径）；
    非流式请求 TTFT 缺失，TPOT 退化为时长/token 均值。

用法：cd scheme-a && set -a && source .env && set +a
      python3 tools/create_dashboards.py [--force]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lf_client import LangfuseClient  # noqa: E402

QUALITY_NAMES = ["tool_calls_total", "tool_errors_total", "steps_total",
                 "tool_error_rate", "redundant_call_ratio"]
PHASE_NAMES = ["task_duration_s", "model_time_s", "non_model_time_s"]
LLM_LAT_NAMES = ["llm_latency_avg_s", "llm_latency_p95_s", "llm_ttft_avg_s",
                 "llm_tpot_avg_s"]
LLM_TOK_NAMES = ["llm_tokens_input", "llm_tokens_output", "llm_tokens_total"]

# ---------- 网格常量（12 列）----------
CARD_H = 3      # 总体数字卡片行高（与手动布局一致）
CHART_H = 6     # 图表行高


def f_eq(column: str, value: str) -> dict:
    return {"type": "string", "column": column, "operator": "=", "value": value}


def f_any(column: str, values: list[str]) -> dict:
    return {"type": "stringOptions", "column": column,
            "operator": "any of", "value": values}


def f_contains(column: str, value: str) -> dict:
    return {"type": "string", "column": column, "operator": "contains",
            "value": value}


def num(view: str, name: str, metrics: list[dict], filters: list[dict],
        description: str = "") -> dict:
    """NUMBER 卡片（无维度）。"""
    return {"view": view, "name": name, "description": description,
            "dimensions": [], "metrics": metrics,
            "filters": filters, "chartType": "NUMBER"}


def chart(view: str, name: str, chart_type: str, metrics: list[dict],
          filters: list[dict], dims: list[dict] | None = None,
          description: str = "") -> dict:
    return {"view": view, "name": name, "description": description,
            "dimensions": dims or [], "metrics": metrics,
            "filters": filters, "chartType": chart_type}


AVG = {"measure": "value", "agg": "avg"}


def card_row(widgets: list[dict], y: float) -> list[tuple[dict, dict]]:
    """一行 4 张 NUMBER 卡片（每张宽 3）。widgets: (widget, 描述) 已建好。"""
    out = []
    for i, w in enumerate(widgets):
        out.append((w, {"x": 3 * i, "y": y, "width": 3, "height": CARD_H}))
    return out


def chart_row(widgets: list[dict], y: float, width: int = 6) -> list[tuple[dict, dict]]:
    out = []
    for i, w in enumerate(widgets):
        out.append((w, {"x": width * i, "y": y, "width": width,
                        "height": CHART_H}))
    return out


def wide(widget: dict, y: float, width: int, x: int = 0,
         height: int = CHART_H) -> tuple[dict, dict]:
    return (widget, {"x": x, "y": y, "width": width, "height": height})


# ---------- 面板 1：评测结果 ----------
score_view = "scores-numeric"

d1_widgets: list[tuple[dict, dict]] = []
d1_widgets += card_row([
    num(score_view, "Trial 总数",
        [{"measure": "value", "agg": "count"}], [f_eq("name", "reward")],
        "reward score 条数 = 已导出 Trial 数"),
    num(score_view, "平均 reward",
        [AVG], [f_eq("name", "reward")],
        "全部 Trial reward 均值（二值 reward 下即通过率）"),
    num(score_view, "reward 中位数",
        [{"measure": "value", "agg": "p50"}], [f_eq("name", "reward")], ""),
    num(score_view, "最差 reward",
        [{"measure": "value", "agg": "min"}], [f_eq("name", "reward")],
        "最差 Trial 得分，暴露短板 case"),
], y=0)
d1_widgets += chart_row([
    chart(score_view, "reward 分布", "HISTOGRAM",
          [{"measure": "value", "agg": "histogram"}],
          [f_eq("name", "reward")],
          description="全体 Trial reward 直方图"),
    chart(score_view, "通过构成（reward=0/1）", "PIE",
          [{"measure": "value", "agg": "count"}],
          [f_eq("name", "reward")], dims=[{"field": "value"}],
          description="通过 vs 未通过 Trial 占比"),
], y=CARD_H)
d1_widgets += [
    wide(chart(score_view, "各 Trial reward", "VERTICAL_BAR",
               [AVG], [f_eq("name", "reward")], dims=[{"field": "traceId"}],
               description="按 Trial 拆解（traceId 对照 export 输出）"),
         y=CARD_H + CHART_H, width=6),
    wide(chart(score_view, "Trial 明细透视", "PIVOT_TABLE",
               [AVG, {"measure": "value", "agg": "min"},
                {"measure": "value", "agg": "max"}],
               [f_eq("name", "reward")], dims=[{"field": "traceId"}],
               description="每 Trial 的 reward avg/min/max"),
         y=CARD_H + CHART_H, width=6, x=6),
]

# ---------- 面板 2：轨迹质量 ----------
d2_widgets: list[tuple[dict, dict]] = []
d2_widgets += card_row([
    num(score_view, "平均工具调用数", [AVG],
        [f_eq("name", "tool_calls_total")], "轨迹客观统计（ATIF）"),
    num(score_view, "平均工具错误率", [AVG],
        [f_eq("name", "tool_error_rate")],
        "observation 命中错误模式的启发式比例"),
    num(score_view, "平均冗余调用率", [AVG],
        [f_eq("name", "redundant_call_ratio")],
        "同签名重复调用占比"),
    num(score_view, "平均步骤数", [AVG],
        [f_eq("name", "steps_total")], "agent step 总数（含无工具调用步骤）"),
], y=0)
d2_widgets += [
    wide(chart(score_view, "各 Trial 工具错误率", "HORIZONTAL_BAR",
               [AVG], [f_eq("name", "tool_error_rate")],
               dims=[{"field": "traceId"}], description="定位错误率高的 case"),
         y=CARD_H, width=6, x=0, height=3),
    wide(chart(score_view, "各 Trial 工具调用数", "VERTICAL_BAR",
               [AVG], [f_eq("name", "tool_calls_total")],
               dims=[{"field": "traceId"}], description="定位调用量大的 case"),
         y=CARD_H, width=6, x=6, height=3),
    wide(chart(score_view, "各 Trial × 质量指标", "PIVOT_TABLE",
               [AVG], [f_any("name", QUALITY_NAMES)],
               dims=[{"field": "traceId"}, {"field": "name"}],
               description="行=Trial，列=指标（唯一明细视图，含错误/步骤绝对数）"),
         y=CARD_H + 3, width=12, x=0),
]

# ---------- 面板 3：时延性能 ----------
d3_widgets: list[tuple[dict, dict]] = []
d3_widgets += card_row([
    num(score_view, "平均任务时长", [AVG],
        [f_eq("name", "task_duration_s")], "轨迹首末时间戳（真值）"),
    num(score_view, "平均模型段", [AVG],
        [f_eq("name", "model_time_s")],
        "proxy GENERATION latency 总和（真值·经 proxy 口径）"),
    num(score_view, "平均非模型段", [AVG],
        [f_eq("name", "non_model_time_s")],
        "残差：任务时长 − 模型段（工具+overhead）"),
    num(score_view, "平均 LLM 请求时延", [AVG],
        [f_eq("name", "llm_latency_avg_s")],
        "单次模型请求 latency 均值（秒）"),
], y=0)
d3_widgets += [
    wide(chart(score_view, "时间构成（模型段 vs 非模型段）", "PIE",
               [{"measure": "value", "agg": "sum"}],
               [f_any("name", ["model_time_s", "non_model_time_s"])],
               dims=[{"field": "name"}],
               description="模型段与非模型段占总时长比例"
                           "（sum 聚合；task_duration = model + non_model）"),
         y=CARD_H, width=6, x=0, height=3),
    wide(chart(score_view, "各 Trial × 三段", "PIVOT_TABLE",
               [AVG], [f_any("name", PHASE_NAMES)],
               dims=[{"field": "traceId"}, {"field": "name"}],
               description="行=Trial，列=task/model/non_model"),
         y=CARD_H, width=6, x=6),
    wide(chart(score_view, "LLM 时延 avg/p95、TTFT 与 TPOT", "HORIZONTAL_BAR",
               [AVG], [f_any("name", LLM_LAT_NAMES)], dims=[{"field": "name"}],
               description="按 Trial 聚合的请求时延均值/p95；"
                           "TTFT/TPOT 为流式真值（经 proxy 口径；"
                           "TTFT 缺失时 TPOT 退化为时长/token 均值）"),
         y=CARD_H + 3, width=6, x=0, height=3),
    wide(chart(score_view, "各 Trial 生成吞吐", "VERTICAL_BAR",
               [AVG], [f_eq("name", "llm_tput_tokens_per_s")],
               dims=[{"field": "traceId"}],
               description="输出 token / 模型段秒数（tokens/s）"),
         y=CARD_H + 6, width=6, x=0),
    wide(chart(score_view, "Token 用量（Trial 均值）", "HORIZONTAL_BAR",
               [AVG], [f_any("name", LLM_TOK_NAMES)], dims=[{"field": "name"}],
               description="输入/输出/合计 token，per-Trial 聚合后再取均值"),
         y=CARD_H + 6, width=6, x=6, height=3),
    wide(chart(score_view, "per-tool 时延 avg/p95", "HORIZONTAL_BAR",
               [AVG, {"measure": "value", "agg": "p95"}],
               [f_contains("name", "interval_s")], dims=[{"field": "name"}],
               description="含该工具 step 的平均间隔（近似，含模型时间）"),
         y=CARD_H + 9, width=6, x=6, height=3),
    wide(chart(score_view, "各 Trial LLM 请求数", "VERTICAL_BAR",
               [AVG], [f_eq("name", "llm_requests")],
               dims=[{"field": "traceId"}],
               description="窗口内 proxy GENERATION 请求数（请求量对比）"),
         y=CARD_H + 12, width=6, x=6, height=3),
]

DASHBOARDS: dict[str, list[tuple[dict, dict]]] = {
    "Harbor · 评测结果": d1_widgets,
    "Harbor · 轨迹质量": d2_widgets,
    "Harbor · 时延性能": d3_widgets,
}

DASH_DESC = {
    "Harbor · 评测结果": "reward 总体概览 + 按 Trial 拆解（scheme-a）",
    "Harbor · 轨迹质量": "轨迹质量三层视图：卡片/分布/明细，指标不重复（scheme-a）",
    "Harbor · 时延性能": "时间构成 / LLM 时延与请求数 / Token / per-tool（scheme-a）",
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="创建指标分类 Dashboard")
    ap.add_argument("--force", action="store_true",
                    help="同名 dashboard 先删除再创建")
    args = ap.parse_args(argv)

    client = LangfuseClient()
    existing = {d["name"]: d["id"] for d in
                client.get("/unstable/dashboards",
                           params={"page": 1, "limit": 100}).get("data", [])}

    for dash_name, placements in DASHBOARDS.items():
        if dash_name in existing:
            if not args.force:
                print(f"[skip] {dash_name}（已存在，--force 可重建）")
                continue
            client._request("DELETE",
                            f"/unstable/dashboards/{existing[dash_name]}",
                            ok=(200, 200, 204))
            print(f"[deleted] {dash_name}")

        dash = client._request(
            "POST", "/unstable/dashboards", ok=(200, 201),
            json={"name": dash_name,
                  "description": DASH_DESC[dash_name]}).json()
        for widget, pos in placements:
            widget = client._request(
                "POST", "/unstable/dashboard-widgets", ok=(200, 201),
                json=widget).json()
            client._request(
                "POST",
                f"/unstable/dashboards/{dash['id']}/placements",
                ok=(200, 201),
                json={"type": "widget", "widgetId": widget["id"], **pos})
            print(f"  [widget] {widget['name']}  "
                  f"@({pos['x']},{pos['y']}) {pos['width']}x{pos['height']}")
        print(f"[created] {dash_name}  id={dash['id']}")

    print("\n查看：Langfuse UI → 项目左侧 Dashboards（"
          "或 /project/<projectId>/dashboards）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
