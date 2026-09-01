import asyncio
import json
import types
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from harbor.trial.hooks import TrialEvent, TrialHookEvent
from harbor_langfuse.plugin import LangfusePlugin


class FakeTaskConfig:
    def __init__(self, task_dir: Path, name="hello-world"):
        self.path = task_dir
        self._name = name

    def get_task_id(self):
        tid = types.SimpleNamespace()
        tid.get_name = lambda: self._name
        tid.model_dump = lambda mode=None: {"name": self._name}
        return tid


class FakeJob:
    def __init__(self, tmp_path):
        self.config = SimpleNamespace(job_name="nightly")
        self.id = uuid4()
        self.job_dir = tmp_path
        self._task_configs = [FakeTaskConfig(tmp_path / "tasks" / "hello-world")]
        self.hooks = {}

    def on_trial_ended(self, cb):
        self.hooks["ended"] = cb
        return self

    def on_trial_cancelled(self, cb):
        self.hooks["cancelled"] = cb
        return self


def _mk_event(tmp_path, rewards=None, exception=False):
    trial_name = "hello-world__agent__model__1"
    trial_dir = tmp_path / trial_name
    (trial_dir / "agent").mkdir(parents=True, exist_ok=True)
    traj = {
        "session_id": "sess-1",
        "agent": {"name": "agent", "model_name": "model"},
        "steps": [
            {
                "step_id": 1,
                "source": "user",
                "message": "hi",
                "timestamp": "2026-09-01T00:00:00Z",
            }
        ],
    }
    (trial_dir / "agent" / "trajectory.json").write_text(json.dumps(traj))

    result = SimpleNamespace(
        verifier_result=SimpleNamespace(rewards=dict(rewards or {})),
        exception_info=None,
        agent_info=SimpleNamespace(
            name="agent", model_info=SimpleNamespace(name="model")
        ),
    )
    if exception:
        result.exception_info = SimpleNamespace(
            exception_type="TimeoutError", exception_message="agent timed out"
        )
    config = SimpleNamespace(
        trial_name=trial_name,
        job_id=str(uuid4()),
        agent=SimpleNamespace(name=None, import_path="x", model_name="model"),
    )
    return TrialHookEvent(
        event=TrialEvent.END,
        task_name="hello-world",
        config=config,
        result=result,
        lock=None,
        timestamp=None,
        _trial_id=uuid4(),
    )


def test_setup_requires_credentials():
    plugin = LangfusePlugin(
        host=None, public_key=None, secret_key=None,
        sync_dataset=False, export_traces=False,
    )
    with pytest.raises(RuntimeError):
        plugin._setup(FakeJob(Path("/tmp/unused")))


def test_setup_requires_host(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    plugin = LangfusePlugin(sync_dataset=False, export_traces=False)
    with pytest.raises(RuntimeError):
        plugin._setup(FakeJob(Path("/tmp/unused")))


def test_finish_trial_exports_spans_and_scores(tmp_path, monkeypatch):
    import requests

    posted = []

    def fake_request(self, method, url, timeout=None, **kw):
        posted.append((method, url, kw.get("json")))
        return SimpleNamespace(status_code=200, json=lambda: {"id": "ds-1"})

    monkeypatch.setattr(requests.Session, "request", fake_request)

    uploads = []
    from harbor_atif2otel.uploaders.langfuse import LangfuseUploader

    monkeypatch.setattr(
        LangfuseUploader, "upload", lambda self, rs: uploads.append(rs)
    )

    monkeypatch.setenv("LANGFUSE_HOST", "http://lf.test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    plugin = LangfusePlugin(dataset_name="ds", experiment_name="exp1")
    job = FakeJob(tmp_path)
    asyncio.run(plugin.on_job_start(job))
    assert "ended" in job.hooks and "cancelled" in job.hooks

    plugin._handle_event_sync(_mk_event(tmp_path, rewards={"reward": 1.0}))

    # trace export happened and spans carry langfuse.* enrichment
    assert len(uploads) == 1
    span = uploads[0].scope_spans[0].spans[0]
    keys = {kv.key for kv in span.attributes}
    assert "langfuse.session.id" in keys
    assert "langfuse.experiment.id" in keys
    assert "langfuse.experiment.dataset.id" in keys

    # one score posted with a traceId derived from the trajectory seed
    score_calls = [(m, u, j) for m, u, j in posted if u.endswith("/scores")]
    assert len(score_calls) == 1
    payload = score_calls[0][2]
    assert payload["name"] == "reward"
    assert payload["value"] == 1.0
    assert payload["dataType"] == "NUMERIC"
    assert payload["traceId"] is not None and len(payload["traceId"]) == 32


def test_exception_trial_posts_boolean_error_score(tmp_path, monkeypatch):
    import requests

    posted = []

    def fake_request(self, method, url, timeout=None, **kw):
        posted.append((method, url, kw.get("json")))
        return SimpleNamespace(status_code=200, json=lambda: {"id": "x"})

    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setenv("LANGFUSE_HOST", "http://lf.test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    plugin = LangfusePlugin(sync_dataset=False, export_traces=False)
    asyncio.run(plugin.on_job_start(FakeJob(tmp_path)))

    plugin._handle_event_sync(_mk_event(tmp_path, rewards={}, exception=True))

    score_calls = [(m, u, j) for m, u, j in posted if u.endswith("/scores")]
    assert len(score_calls) == 1
    payload = score_calls[0][2]
    assert payload["name"] == "harbor_error"
    assert payload["dataType"] == "BOOLEAN"


def test_dataset_sync_creates_and_upserts_items(tmp_path, monkeypatch):
    import requests

    calls = []

    def fake_request(self, method, url, timeout=None, **kw):
        calls.append((method, url, kw.get("json")))
        if method == "GET" and url.endswith("/datasets/ds"):
            return SimpleNamespace(status_code=200, json=lambda: {"id": "ds-1"})
        return SimpleNamespace(status_code=201, json=lambda: {"id": "item-1"})

    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setenv("LANGFUSE_HOST", "http://lf.test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    plugin = LangfusePlugin(dataset_name="ds", export_traces=False)
    job = FakeJob(tmp_path)
    (tmp_path / "tasks" / "hello-world" / "instruction.md").write_text("Do it.")
    asyncio.run(plugin.on_job_start(job))

    assert plugin._dataset_id == "ds-1"
    item_calls = [(m, u, j) for m, u, j in calls if u.endswith("/dataset-items")]
    assert len(item_calls) == 1
    payload = item_calls[0][2]
    assert payload["datasetName"] == "ds"
    assert payload["input"]["instruction"] == "Do it."
