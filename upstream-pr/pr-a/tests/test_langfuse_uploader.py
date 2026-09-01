from unittest.mock import MagicMock, patch

import base64

from harbor_atif2otel import convert_trajectory
from harbor_atif2otel.uploaders.langfuse import LangfuseUploader


def _mk_uploader(**kw):
    defaults = dict(
        host="https://langfuse.example.com/",
        public_key="pk-lf-test",
        secret_key="sk-lf-test",
        throttle_seconds=0,
    )
    defaults.update(kw)
    return LangfuseUploader(**defaults)


def _mock_response(status: int, body: bytes = b""):
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_construction_strips_trailing_slash():
    u = _mk_uploader()
    assert u.endpoint == "https://langfuse.example.com/api/public/otel/v1/traces"


def test_base_headers():
    u = _mk_uploader()
    headers = u._base_headers()
    expected = base64.b64encode(b"pk-lf-test:sk-lf-test").decode()
    assert headers["Authorization"] == f"Basic {expected}"
    assert headers["x-langfuse-ingestion-version"] == "4"


def test_base_headers_custom_ingestion_version():
    u = _mk_uploader(ingestion_version="3")
    assert u._base_headers()["x-langfuse-ingestion-version"] == "3"


@patch("harbor_atif2otel.uploaders.langfuse.urlopen")
def test_upload_sends_protobuf_with_auth(mock_urlopen, trajectory_pass):
    mock_urlopen.return_value = _mock_response(200)

    u = _mk_uploader()
    rs = convert_trajectory(trajectory_pass)
    u.upload(rs)

    assert mock_urlopen.called
    req = mock_urlopen.call_args[0][0]
    assert req.full_url == "https://langfuse.example.com/api/public/otel/v1/traces"
    assert req.get_header("Content-type") == "application/x-protobuf"
    assert req.get_header("X-langfuse-ingestion-version") == "4"
    expected = base64.b64encode(b"pk-lf-test:sk-lf-test").decode()
    assert req.get_header("Authorization") == f"Basic {expected}"
    assert len(req.data) > 0


@patch("harbor_atif2otel.uploaders.langfuse.time.sleep")
@patch("harbor_atif2otel.uploaders.langfuse.urlopen")
def test_upload_retries_on_503(mock_urlopen, mock_sleep, trajectory_pass):
    mock_urlopen.side_effect = [
        _mock_response(503, b"busy"),
        _mock_response(200),
    ]

    u = _mk_uploader(max_retries=3)
    rs = convert_trajectory(trajectory_pass)
    u.upload(rs)  # should not raise

    assert mock_urlopen.call_count == 2
    assert mock_sleep.called


@patch("harbor_atif2otel.uploaders.langfuse.time.sleep")
@patch("harbor_atif2otel.uploaders.langfuse.urlopen")
def test_upload_raises_after_retries(mock_urlopen, mock_sleep, trajectory_pass):
    mock_urlopen.side_effect = [_mock_response(500, b"boom")] * 5

    u = _mk_uploader(max_retries=2)
    rs = convert_trajectory(trajectory_pass)
    try:
        u.upload(rs)
        raised = False
    except RuntimeError as e:
        raised = True
        assert "HTTP 500" in str(e)
    assert raised, "expected RuntimeError"
    assert mock_urlopen.call_count == 3  # initial + 2 retries


@patch("harbor_atif2otel.uploaders.langfuse.urlopen")
def test_upload_does_not_retry_on_401(mock_urlopen, trajectory_pass):
    mock_urlopen.side_effect = [_mock_response(401, b"unauthorized")]

    u = _mk_uploader(max_retries=3)
    rs = convert_trajectory(trajectory_pass)
    try:
        u.upload(rs)
        raised = False
    except RuntimeError as e:
        raised = True
        assert "HTTP 401" in str(e)
    assert raised, "expected RuntimeError"
    assert mock_urlopen.call_count == 1  # auth errors are not retryable
