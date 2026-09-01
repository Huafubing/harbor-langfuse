#!/usr/bin/env bash
set -e
S=/mnt/c/Users/Libo/.trae-cn/work/6a8d77ca0bf12d45946eee43/harbor-langfuse-stage/pr-a
D=/root/harbor-upstream/packages/harbor-atif2otel
cp "$S/langfuse.py" "$D/src/harbor_atif2otel/uploaders/langfuse.py"
cp "$S/test_langfuse_uploader.py" "$D/tests/"
cp "$S/plugin.py" "$D/src/harbor_atif2otel/plugin.py"
cp "$S/README.md" "$D/README.md"
find "$D" -name '*.py' -exec sed -i 's/\r$//' {} +
echo APPLIED
cd "$D"
pip3 install -q pytest pytest-asyncio 'opentelemetry-proto>=1.42.1' 2>&1 | tail -1
python3 -m py_compile src/harbor_atif2otel/plugin.py src/harbor_atif2otel/uploaders/langfuse.py
echo SYNTAX_OK
PYTHONPATH=src python3 -m pytest tests/test_langfuse_uploader.py tests/test_mlflow_uploader.py tests/test_convert.py tests/test_validate.py tests/test_ids.py tests/test_export.py -q 2>&1 | tail -12
