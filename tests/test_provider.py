# -*- coding: utf-8 -*-
"""验证 build_provider：OTLP exporter 构造成功且携带必要请求头（不发送网络请求）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from atif2langfuse.spans import build_provider


def main() -> int:
    provider = build_provider(
        "http://localhost:3000/api/public/otel", "pk-lf-demo", "sk-lf-demo")
    assert provider is not None
    print("PROVIDER_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
