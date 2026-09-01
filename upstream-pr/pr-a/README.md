# PR-A: LangfuseUploader (harbor-atif2otel)

RFC 0001 的第一个 PR 的完整实现，基于 harbor-framework/harbor main@c0acdfbf 的工作台验证。

## 文件
- `src/harbor_atif2otel/uploaders/langfuse.py` — 新增 LangfuseUploader（OTLP/HTTP + Basic 认证 + x-langfuse-ingestion-version: 4 + 408/429/5xx 指数退避）
- `src/harbor_atif2otel/plugin.py` — `_make_uploader()` 自动探测（LANGFUSE_PUBLIC_KEY 存在即选 Langfuse；LANGFUSE_HOST 可替代 OTEL_EXPORTER_OTLP_ENDPOINT 满足 auto 模式端点要求）
- `tests/test_langfuse_uploader.py` — 7 个单测（端点/认证头/protobuf 体积/重试/不可重试错误）
- `0001-langfuse-uploader.patch` — 相对上游 main 的 unified diff（PR 直接可用）

## 验证
Python 3.10 下 77/77 通过（langfuse 7 + mlflow 7 + convert 17 + validate 11 + ids 11 + export 24）。
`test_plugin.py` 需要 Python 3.12（`typing.override`），建议 PR 时在 3.12 环境补跑。
