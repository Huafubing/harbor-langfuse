# -*- coding: utf-8 -*-
"""清理 harbortrial 项目中的评测调试遗留数据。

删除两类对象（Langfuse public API，应用层级联/队列删除）：
  1. 孤儿 scores —— trace_id 不存在于 traces 表的历史 score
     （45 条旧 case 只 POST 了 score、未上传 trace）
  2. 非本次评测的 traces —— tags 既不含 harbor 也不含
     --keep-tag（trial id）的调试冒烟 trace

保留：trial trace（tags 含 harbor）+ join 过的 proxy generations
（tags 含 trial id）及其上的 scores。

用法：cd scheme-a && set -a && source .env && set +a
      python3 tools/cleanup_orphans.py --keep-tag hello-world__KLSZT2P
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lf_client import LangfuseClient  # noqa: E402


def ch_rows(query: str) -> list[dict]:
    """ClickHouse 只读查询（docker exec，JSONEachRow）。"""
    r = subprocess.run(
        ["docker", "exec", "langfuse-clickhouse-1", "clickhouse-client",
         "--user", "clickhouse", "--password", "clickhouse",
         "--query", query, "--format", "JSONEachRow"],
        capture_output=True, text=True, check=True)
    return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="清理孤儿 scores 与调试 traces")
    ap.add_argument("--keep-tag", required=True,
                    help="保留 tags 含此 trial id 的 trace（如 hello-world__KLSZT2P）")
    ap.add_argument("--sleep", type=float, default=5.0,
                    help="删除后等待队列消费的秒数")
    args = ap.parse_args(argv)

    client = LangfuseClient()

    # 1) 孤儿 scores：挂在悬空 trace_id 上。逐条 DELETE /scores 走标记队列太慢，
    #    改按 trace_id 删 trace —— worker 会 mutation 级联删其下全部
    #    scores/observations（deleteScoresByTraceIds），速度快得多。
    orphan_traces = ch_rows(
        "SELECT DISTINCT trace_id FROM default.scores FINAL "
        "WHERE trace_id NOT IN (SELECT id FROM default.traces FINAL)")
    print(f"悬空 trace_id（孤儿 scores 挂载）：{len(orphan_traces)} 个")
    ok = fail = 0
    for t in orphan_traces:
        try:
            client._request("DELETE", f"/traces/{t['trace_id']}", ok=(200,))
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  [fail] trace_id={t['trace_id']}: {e}")
    print(f"已受理 {ok} 个，失败 {fail} 个")

    # 2) 调试 traces：tags 无 harbor 且无 trial id
    keep = (f"hasAny(tags, ['harbor', '{args.keep_tag}'])")
    traces = ch_rows(
        f"SELECT id, name FROM default.traces FINAL WHERE NOT {keep}")
    print(f"待删 traces：{len(traces)} 条")
    for t in traces:
        try:
            client._request("DELETE", f"/traces/{t['id']}", ok=(200,))
            print(f"  [deleted] {t['name']}  id={t['id']}")
        except Exception as e:  # noqa: BLE001
            print(f"  [fail] {t['name']} id={t['id']}: {e}")

    # 3) 等队列消费后验证
    print(f"等待 {args.sleep}s 让删除队列消费...")
    time.sleep(args.sleep)
    scores_left = ch_rows(
        "SELECT name, count() AS cnt FROM default.scores FINAL "
        "GROUP BY name ORDER BY name")
    traces_left = ch_rows(
        "SELECT count() AS cnt FROM default.traces FINAL")
    print("剩余 scores：", {r["name"]: int(r["cnt"]) for r in scores_left})
    print(f"剩余 traces：{traces_left[0]['cnt']} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
