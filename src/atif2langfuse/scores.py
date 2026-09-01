# -*- coding: utf-8 -*-
"""Scores 回填：走 Langfuse Public API（REST），避免 SDK 版本差异。

POST {host}/api/public/score
body: {"traceId": "...", "observationId": "...",
       "name": "...", "value": ..., "dataType": "NUMERIC"|"CATEGORICAL"|"BOOLEAN"|"TEXT"}

注意：CATEGORICAL 分值（如 phenotype）若未在 UI 预先创建同名 score config，
部分版本会拒绝写入；此时先在 Langfuse 界面创建同名 CATEGORICAL 配置。
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import requests


def _score_items(trial) -> List[Dict[str, object]]:
    items: List[Dict[str, object]] = []
    if trial.reward is not None:
        items.append({"name": "reward",
                      "value": float(trial.reward),
                      "dataType": "NUMERIC"})
    for name, value in (trial.reward_details or {}).items():
        try:
            items.append({"name": f"reward.{name}",
                          "value": float(value),
                          "dataType": "NUMERIC"})
        except (TypeError, ValueError):
            continue
    for tag in trial.phenotypes or []:
        items.append({"name": "phenotype",
                      "value": str(tag),
                      "dataType": "CATEGORICAL"})
    return items


def backfill_scores(host: str, public_key: str, secret_key: str,
                    ids: Dict[str, str], trial, timeout: int = 15
                    ) -> List[Tuple[str, object, str]]:
    url = host.rstrip("/") + "/api/public/score"
    auth = (public_key, secret_key)
    results: List[Tuple[str, object, str]] = []
    for item in _score_items(trial):
        payload = {"traceId": ids["trace_id"],
                   "observationId": ids["observation_id"],
                   **item}
        try:
            resp = requests.post(url, json=payload, auth=auth, timeout=timeout)
            status = resp.status_code
            info = "ok" if status < 300 else resp.text[:160]
        except requests.RequestException as exc:
            status, info = None, str(exc)[:160]
        results.append((str(item["name"]), status, info))
    return results
