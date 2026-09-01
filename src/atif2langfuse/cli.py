# -*- coding: utf-8 -*-
"""CLI：atif2langfuse list | export。

list        发现并展示 Trial（无需任何第三方依赖）
export      构建 span 计划并上报；--dry-run 可离线验证
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Settings
from .reader import load_trials
from .sanitize import sanitize
from .spans import build_span_plan


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--run-root", required=True,
                   help="Harbor run 产物根目录（含若干 Trial 子目录）")
    p.add_argument("--mode", choices=["structural", "full"],
                   help="内容模式，默认 structural")
    p.add_argument("--filter", dest="trial_filter",
                   choices=["all", "success", "failure"],
                   help="按 reward 过滤 Trial")
    p.add_argument("--limit", type=int, help="最多处理 N 个 Trial")


def _build_settings(args: argparse.Namespace) -> Settings:
    s = Settings.from_env()
    if args.mode:
        s.mode = args.mode
    if args.trial_filter:
        s.trial_filter = args.trial_filter
    if getattr(args, "host", None):
        s.langfuse_host = args.host
    if getattr(args, "pk", None):
        s.public_key = args.pk
    if getattr(args, "sk", None):
        s.secret_key = args.sk
    if getattr(args, "dataset_id", None):
        s.dataset_id = args.dataset_id
    if getattr(args, "run_name", None):
        s.run_name = args.run_name
    s.skip_scores = bool(getattr(args, "skip_scores", False))
    return s


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="atif2langfuse",
        description="把 Harbor ATIF 轨迹与判分导出到自托管 Langfuse（OTel 语义轨）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出发现的 Trial 及判分")
    _add_common(p_list)

    p_exp = sub.add_parser("export", help="导出 span 并回填 Scores")
    _add_common(p_exp)
    p_exp.add_argument("--host", help="Langfuse 地址，如 http://localhost:3000")
    p_exp.add_argument("--pk", help="Public key（pk-lf-...）")
    p_exp.add_argument("--sk", help="Secret key（sk-lf-...）")
    p_exp.add_argument("--dataset-id", help="关联 Langfuse Dataset（可选）")
    p_exp.add_argument("--run-name", help="Experiment 展示名（可选）")
    p_exp.add_argument("--dry-run", action="store_true",
                       help="只构建计划不上报（无需网络与第三方依赖）")
    p_exp.add_argument("--skip-scores", action="store_true",
                       help="跳过 Scores 回填")

    args = parser.parse_args(argv)
    settings = _build_settings(args)

    root = Path(args.run_root)
    if not root.exists():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 2

    trials, skipped = load_trials(root,
                                  trial_filter=settings.trial_filter,
                                  limit=args.limit,
                                  run_id_hint=root.name)
    print(f"发现 Trial：{len(trials)}（解析失败跳过 {skipped}）")
    if not trials:
        return 0

    if args.command == "list":
        for t in trials:
            reward = "n/a" if t.reward is None else f"{t.reward:.3f}"
            print(f"  {t.trial_dir.name}\n"
                  f"    task={t.task_id} agent={t.agent_name} "
                  f"model={t.model_name} attempt={t.attempt}\n"
                  f"    reward={reward} steps={len(t.steps)} "
                  f"scores={list(t.reward_details) or '-'}")
        return 0

    # ---------- export ----------
    provider = None
    tracer = None
    if not args.dry_run:
        try:
            from .spans import build_provider
        except ImportError:
            print('缺少 OTel 依赖：pip install -e ".[otel]"', file=sys.stderr)
            return 3
        settings.validate_for_export()
        provider = build_provider(settings.otel_endpoint,
                                  settings.public_key, settings.secret_key)
        tracer = provider.get_tracer("atif2langfuse")

    total_spans = 0
    exported: list = []
    failures = 0
    for t in trials:
        plan = build_span_plan(t, mode=settings.mode,
                               run_name=settings.run_name,
                               dataset_id=settings.dataset_id)
        total_spans += len(plan)
        if args.dry_run:
            roles: dict = {}
            for spec in plan:
                roles[spec.role] = roles.get(spec.role, 0) + 1
            print(f"  [dry-run] {t.trial_dir.name}: spans={len(plan)} {roles}")
            print(f"            input({settings.mode}) 样例: "
                  f"{sanitize(t.task_prompt, settings.mode)!r}")
            continue
        try:
            from .spans import execute_plan
            ids = execute_plan(tracer, plan)
            exported.append((t, ids))
            print(f"  [exported] {t.trial_dir.name} trace_id={ids['trace_id']}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  [error] {t.trial_dir.name}: {exc}", file=sys.stderr)

    if args.dry_run:
        print(f"dry-run 完成：{len(trials)} Trial / {total_spans} span（未联网上报）")
        return 0

    try:
        provider.force_flush()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] force_flush: {exc}", file=sys.stderr)

    if not settings.skip_scores:
        from .scores import backfill_scores
        for t, ids in exported:
            for name, status, info in backfill_scores(
                    settings.langfuse_host, settings.public_key,
                    settings.secret_key, ids, t):
                if status is not None and status < 300:
                    print(f"  [score:ok] {t.trial_dir.name} {name}")
                else:
                    print(f"  [score:fail] {t.trial_dir.name} {name} "
                          f"http={status} {info}", file=sys.stderr)

    try:
        provider.shutdown()
    except Exception:  # noqa: BLE001
        pass
    print(f"导出完成：成功 {len(exported)} / 失败 {failures}，"
          f"共 {total_spans} span")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
