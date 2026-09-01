# -*- coding: utf-8 -*-
"""配置：环境变量优先，CLI 参数覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class Settings:
    langfuse_host: str = "http://localhost:3000"
    public_key: str = ""
    secret_key: str = ""
    mode: str = "structural"          # full | structural
    trial_filter: str = "all"         # all | success | failure
    dataset_id: Optional[str] = None
    run_name: Optional[str] = None
    skip_scores: bool = False

    @property
    def otel_endpoint(self) -> str:
        return self.langfuse_host.rstrip("/") + "/api/public/otel"

    def validate_for_export(self) -> None:
        missing = [k for k, v in (
            ("LANGFUSE_HOST", self.langfuse_host),
            ("LANGFUSE_PUBLIC_KEY", self.public_key),
            ("LANGFUSE_SECRET_KEY", self.secret_key),
        ) if not v]
        if missing:
            raise SystemExit(
                "缺少配置：%s（.env / 环境变量 / CLI 参数任选其一提供）" % ", ".join(missing))

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            langfuse_host=os.environ.get("LANGFUSE_HOST", cls.langfuse_host),
            public_key=os.environ.get("LANGFUSE_PUBLIC_KEY", ""),
            secret_key=os.environ.get("LANGFUSE_SECRET_KEY", ""),
            mode=os.environ.get("ATIF2LF_MODE", cls.mode),
            trial_filter=os.environ.get("ATIF2LF_FILTER", cls.trial_filter),
            dataset_id=os.environ.get("ATIF2LF_DATASET_ID") or None,
            run_name=os.environ.get("ATIF2LF_RUN_NAME") or None,
        )
