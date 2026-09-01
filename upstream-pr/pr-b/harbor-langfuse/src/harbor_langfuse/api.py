"""Thin REST client for the Langfuse Public API.

All endpoints live under ``{host}/api/public`` and authenticate with HTTP
Basic auth using the project's public/secret key pair. Only a small subset
of the API is implemented here — the minimum needed by the plugin:

- ``POST /datasets``                       create a dataset
- ``GET  /datasets/{name}``                fetch a dataset by name
- ``POST /dataset-items``                  upsert a dataset item (by ``id``)
- ``POST /scores``                         create/upsert a score (by ``id``)
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


class LangfuseRestClient:
    """REST client for the Langfuse Public API with retry/backoff.

    Args:
        host: Langfuse base URL (e.g. ``https://langfuse.example.com``).
        public_key: Langfuse public key (``pk-lf-...``).
        secret_key: Langfuse secret key (``sk-lf-...``).
        timeout_seconds: per-request timeout (default 30).
        max_retries: retries on connection errors and 408/429/5xx (default 3).
        retry_delay_seconds: base delay for exponential backoff (default 1.0).
    """

    def __init__(
        self,
        host: str,
        public_key: str,
        secret_key: str,
        *,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        retry_delay_seconds: float = 1.0,
    ):
        self._base_url = host.rstrip("/") + "/api/public"
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._retry_delay = retry_delay_seconds
        self._session = requests.Session()
        self._session.auth = (public_key, secret_key)

    def request(
        self,
        method: str,
        path: str,
        *,
        ok_statuses: tuple[int, ...] = (200,),
        **kwargs: Any,
    ) -> requests.Response:
        """Perform a request against ``{host}/api/public``.

        Retries on connection errors and retryable status codes; raises for
        status when the final response is not in ``ok_statuses``.
        """
        url = f"{self._base_url}{path}"
        attempts = self._max_retries + 1
        for attempt in range(attempts):
            try:
                response = self._session.request(
                    method, url, timeout=self._timeout, **kwargs
                )
            except requests.RequestException:
                if attempt < self._max_retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise

            if response.status_code in ok_statuses:
                return response
            if (
                response.status_code in _RETRYABLE_STATUS_CODES
                and attempt < self._max_retries
            ):
                self._sleep_before_retry(attempt)
                continue
            response.raise_for_status()
            return response
        msg = "Langfuse request retry loop exhausted unexpectedly"
        raise RuntimeError(msg)

    def _sleep_before_retry(self, attempt: int) -> None:
        if self._retry_delay <= 0:
            return
        time.sleep(self._retry_delay * (2**attempt))
