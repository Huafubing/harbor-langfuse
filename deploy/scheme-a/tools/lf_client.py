# -*- coding: utf-8 -*-
"""Langfuse Public API 轻量客户端（三工具共享）。

读取环境变量：LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
（或显式传参覆盖）。
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests

RETRYABLE = frozenset({408, 429, 500, 502, 503, 504})


class LangfuseClient:
    def __init__(self, host: str | None = None, pk: str | None = None,
                 sk: str | None = None, timeout: float = 30.0,
                 retries: int = 3, retry_delay: float = 1.0):
        self.base = (host or os.environ.get("LANGFUSE_HOST", "http://localhost:3000")
                     ).rstrip("/") + "/api/public"
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay
        self.session = requests.Session()
        self.session.auth = (pk or os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
                             sk or os.environ.get("LANGFUSE_SECRET_KEY", ""))

    def _request(self, method: str, path: str, ok: tuple[int, ...] = (200,),
                 **kwargs) -> requests.Response:
        url = f"{self.base}{path}"
        last: requests.Response | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.request(method, url, timeout=self.timeout,
                                            **kwargs)
            except requests.RequestException:
                if attempt < self.retries:
                    time.sleep(self.retry_delay * (2 ** attempt))
                    continue
                raise
            if resp.status_code in ok:
                return resp
            if resp.status_code in RETRYABLE and attempt < self.retries:
                time.sleep(self.retry_delay * (2 ** attempt))
                continue
            resp.raise_for_status()
            return resp
        raise RuntimeError("retry loop exhausted")

    def get(self, path: str, params: dict | None = None) -> Any:
        return self._request("GET", path, params=params).json()

    def post_score(self, body: dict) -> Any:
        """POST /scores（CreateScoreRequest：traceId/dataType/value camelCase）。"""
        return self._request("POST", "/scores", ok=(200, 201), json=body).json()

    def patch_trace(self, trace_id: str, body: dict) -> Any:
        return self._request("PATCH", f"/traces/{trace_id}",
                             ok=(200, 202, 204), json=body).json()

    # ---------- 高层查询 ----------

    def list_traces(self, page_size: int = 50, **params) -> list[dict]:
        """分页遍历 GET /traces 的 data 数组。"""
        out: list[dict] = []
        page = 1
        while True:
            params_full = {"page": page, "limit": page_size, **params}
            data = self.get("/traces", params=params_full)
            batch = data.get("data") or []
            out.extend(batch)
            if len(batch) < page_size:
                return out
            page += 1

    def find_trial_trace(self, session_id: str, from_ts: str | None = None,
                         to_ts: str | None = None) -> dict | None:
        """按 sessionId + harbor tag 定位 trial 轨迹 trace（取最新一条）。"""
        params: dict[str, Any] = {"tags": "harbor"}
        if from_ts:
            params["fromTimestamp"] = from_ts
        if to_ts:
            params["toTimestamp"] = to_ts
        hits = [t for t in self.list_traces(**params)
                if t.get("sessionId") == session_id]
        if not hits:
            return None
        hits.sort(key=lambda t: t.get("timestamp") or "")
        return hits[-1]

    def trace_scores(self, trace_id: str) -> list[dict]:
        return self.get(f"/traces/{trace_id}").get("scores") or []
