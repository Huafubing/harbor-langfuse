"""Langfuse OTLP uploader (OTLP/HTTP with protobuf body)."""

from __future__ import annotations

import base64
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans

from .base import Uploader

_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


class LangfuseUploader(Uploader):
    """Upload OTel spans to Langfuse via OTLP/HTTP.

    Langfuse ingests OpenTelemetry spans on
    ``POST {host}/api/public/otel/v1/traces`` using HTTP Basic auth with the
    project's public/secret key pair. Sending the
    ``x-langfuse-ingestion-version: 4`` header opts into real-time ingestion
    on Langfuse v4; without it, directly-ingested OTel data can be delayed by
    up to 10 minutes.

    Args:
        host: Langfuse base URL (e.g. ``https://langfuse.example.com``).
        public_key: Langfuse public key (``pk-lf-...``).
        secret_key: Langfuse secret key (``sk-lf-...``).
        ingestion_version: value for the ``x-langfuse-ingestion-version``
            header (default ``"4"``).
        timeout_seconds: per-request timeout (default 30).
        max_retries: retries on 408/429/5xx with exponential backoff
            (default 3).
        throttle_seconds: sleep between uploads (default 0).
    """

    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        ingestion_version: str = "4",
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        throttle_seconds: float = 0.0,
    ):
        self._host = host.rstrip("/")
        self._public_key = public_key
        self._secret_key = secret_key
        self._ingestion_version = ingestion_version
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._throttle = throttle_seconds

    @property
    def endpoint(self) -> str:
        return f"{self._host}/api/public/otel/v1/traces"

    def _base_headers(self) -> dict[str, str]:
        auth = base64.b64encode(
            f"{self._public_key}:{self._secret_key}".encode()
        ).decode()
        return {
            "Authorization": f"Basic {auth}",
            "x-langfuse-ingestion-version": self._ingestion_version,
        }

    def upload(self, resource_spans: ResourceSpans) -> None:
        request = ExportTraceServiceRequest(resource_spans=[resource_spans])
        body = request.SerializeToString()

        attempt = 0
        while True:
            status, resp = self._post(self.endpoint, body)
            if status in (200, 201, 204):
                break
            if status in _RETRYABLE_STATUS_CODES and attempt < self._max_retries:
                time.sleep(min(2**attempt, 8))
                attempt += 1
                continue
            raise RuntimeError(
                f"Langfuse OTLP upload failed: HTTP {status}: {resp[:500]}"
            )

        if self._throttle > 0:
            time.sleep(self._throttle)

    def _post(self, url: str, body: bytes) -> tuple[int, str]:
        headers = {
            **self._base_headers(),
            "Content-Type": "application/x-protobuf",
        }
        req = Request(url, data=body, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=self._timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="replace")
        except URLError as e:
            return 0, str(e)
