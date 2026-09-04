# -*- coding: utf-8 -*-
"""Langfuse Public API 轻量客户端（三工具共享）。

读取环境变量：LANGFUSE_HOST / LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
（或显式传参覆盖）。
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
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
        """v4：PATCH /traces/{id} 已移除（405），改走 ingestion
        trace-create upsert（部分 body 合并，不覆盖原 name/input/output；
        v2 侧 events 表 merge 约 5s，v1 /traces 读 v3 表立即可见）。"""
        now = datetime.now(timezone.utc).isoformat()
        event = {"id": str(uuid.uuid4()), "timestamp": now,
                 "type": "trace-create",
                 "body": {"id": trace_id, "timestamp": now, **body}}
        return self._request("POST", "/ingestion", ok=(200, 201, 202, 207),
                             json={"batch": [event]}).json()

    def trace_scores(self, trace_id: str) -> list[dict]:
        """GET /v3/scores?traceId=（v4 events_only 下 GET /traces/{id} 已移除）。"""
        out: list[dict] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"traceId": trace_id, "limit": 50}
            if cursor:
                params["cursor"] = cursor
            data = self.get("/v3/scores", params=params)
            out.extend(data.get("data") or [])
            cursor = (data.get("meta") or {}).get("cursor")
            if not cursor or not data.get("data"):
                return out

    # ---------- 高层查询 ----------
    # 注：Langfuse v4 下读侧统一走 GET /v2/observations（events 表）：
    # - trace 列表 = root observation（isRootObservation=true）合成，每 trace 一行；
    # - sessionId / fromStartTime / toStartTime 为服务端过滤；
    # - tags 来自 trace_context 字段组。

    def _iter_observations(self, params: dict) -> list[dict]:
        out: list[dict] = []
        cursor: str | None = None
        while True:
            full = dict(params)
            full.setdefault("limit", 50)
            if cursor:
                full["cursor"] = cursor
            data = self.get("/v2/observations", params=full)
            batch = data.get("data") or []
            out.extend(batch)
            cursor = (data.get("meta") or {}).get("cursor")
            if not cursor or not batch:
                return out

    @staticmethod
    def _iso_window(from_ts: str | None, to_ts: str | None) -> dict:
        q: dict[str, Any] = {}
        if from_ts:
            q["fromStartTime"] = from_ts
        if to_ts:
            q["toStartTime"] = to_ts
        return q

    def list_traces(self, session_id: str | None = None,
                    from_ts: str | None = None, to_ts: str | None = None,
                    page_size: int = 50) -> list[dict]:
        """v4 兼容：root observations 合成 trace 列表（每 trace 一行）。
        sessionId / from_ts / to_ts 均为服务端过滤；tags 取 trace_context 组。"""
        q: dict[str, Any] = {
            "isRootObservation": "true",
            "fields": "core,basic,trace_context",
            "limit": page_size,
            **self._iso_window(from_ts, to_ts),
        }
        if session_id:
            q["sessionId"] = session_id
        return [{"id": o.get("traceId"), "name": o.get("name"),
                 "sessionId": o.get("sessionId"),
                 "timestamp": o.get("startTime"), "tags": o.get("tags") or []}
                for o in self._iter_observations(q)]

    def list_generations(self, session_id: str | None = None,
                         trace_id: str | None = None,
                         from_ts: str | None = None,
                         to_ts: str | None = None,
                         page_size: int = 50) -> list[dict]:
        """v4 兼容：列 GENERATION observation（proxy 模型请求）。
        原生字段：latency / timeToFirstToken（秒，metrics 组）、
        usageDetails.output（usage 组）、completionStartTime（time 组）。
        注：sessionId 过滤命中 events 表反规范化列（ingest 时固化），
        join PATCH 之后的 proxy generations 需改用 trace_id= 查询。"""
        q: dict[str, Any] = {
            "type": "GENERATION",
            "fields": "core,metrics,usage,time,trace_context",
            "limit": page_size,
            **self._iso_window(from_ts, to_ts),
        }
        if session_id:
            q["sessionId"] = session_id
        if trace_id:
            q["traceId"] = trace_id
        return [{"traceId": o.get("traceId"), "startTime": o.get("startTime"),
                 "latency": o.get("latency"),
                 "timeToFirstToken": o.get("timeToFirstToken"),
                 "completionStartTime": o.get("completionStartTime"),
                 "inputTokens": (o.get("usageDetails") or {}).get("input"),
                 "outputTokens": (o.get("usageDetails") or {}).get("output"),
                 "tags": o.get("tags") or []}
                for o in self._iter_observations(q)]

    def find_trial_trace(self, session_id: str) -> dict | None:
        """v4 兼容：按 sessionId 定位 trial 轨迹 trace（root observation
        name=trial，取最新一条）。返回含 id=traceId 的 dict。"""
        obs = self._iter_observations(
            {"name": "trial", "sessionId": session_id})
        if not obs:
            return None
        obs.sort(key=lambda o: o.get("startTime") or "")
        latest = obs[-1]
        return {"id": latest.get("traceId"), "sessionId": session_id,
                "timestamp": latest.get("startTime")}

    def list_traces_v1(self, session_id: str, limit: int = 100) -> list[dict]:
        """v1 GET /traces（默认 legacy 路径读 v3 traces 表）。
        join 的 sessionId PATCH 写的就是这张表（worker 异步 merge，
        约数秒后可见）；v2 observations 的 sessionId 过滤命中
        events 表固化列，看不到 PATCH。events_only 写模式下不可用。"""
        return self.get("/traces", params={"sessionId": session_id,
                                           "limit": limit}).get("data") or []
