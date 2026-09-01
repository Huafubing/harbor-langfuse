import pytest

from harbor_langfuse.api import LangfuseRestClient
from harbor_langfuse.scores import (
    build_exception_score,
    build_score_items,
    post_scores,
    stable_score_id,
)


def test_build_score_items_numeric_and_categorical():
    items = build_score_items(
        {"reward": 1.0, "phenotype": "E3"},
        trace_id="t" * 32,
        observation_id="o" * 16,
        id_scope="exp:trial-1",
    )
    assert len(items) == 2
    by_name = {i["name"]: i for i in items}
    reward = by_name["reward"]
    assert reward["value"] == 1.0
    assert reward["dataType"] == "NUMERIC"
    assert reward["traceId"] == "t" * 32
    assert reward["observationId"] == "o" * 16
    assert reward["metadata"]["source"] == "harbor"
    phen = by_name["phenotype"]
    assert phen["value"] == "E3"
    assert phen["dataType"] == "CATEGORICAL"


def test_score_ids_are_stable_and_scoped():
    a = build_score_items({"reward": 1.0}, id_scope="exp1:trial-1")
    b = build_score_items({"reward": 1.0}, id_scope="exp1:trial-1")
    c = build_score_items({"reward": 1.0}, id_scope="exp2:trial-1")
    assert a[0]["id"] == b[0]["id"]
    assert a[0]["id"] != c[0]["id"]
    assert a[0]["id"] == stable_score_id("exp1:trial-1", "reward")


def test_build_exception_score():
    item = build_exception_score(
        trace_id="t" * 32, id_scope="exp:trial", message="boom"
    )
    assert item["name"] == "harbor_error"
    assert item["value"] == 1.0
    assert item["dataType"] == "BOOLEAN"
    assert "boom" in item["comment"]


def test_post_scores_counts_ok_and_failed():
    class FakeClient:
        def __init__(self, fail_on_names=()):
            self.fail_on_names = fail_on_names
            self.calls = []

        def request(self, method, path, *, ok_statuses=None, json=None):
            self.calls.append((method, path, json))
            if json["name"] in self.fail_on_names:
                raise RuntimeError("boom")
            return None

    client = FakeClient(fail_on_names={"b"})
    items = [
        {"id": "1", "name": "a", "value": 1.0, "dataType": "NUMERIC"},
        {"id": "2", "name": "b", "value": 0.5, "dataType": "NUMERIC"},
        {"id": "3", "name": "c", "value": 0.25, "dataType": "NUMERIC"},
    ]
    ok, failed = post_scores(client, items)
    assert (ok, failed) == (2, 1)
    assert len(client.calls) == 3
    assert all(method == "POST" and path == "/scores" for method, path, _ in client.calls)


def test_client_retries_then_succeeds(monkeypatch):
    import requests

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"HTTP {self.status_code}")

    calls = []

    def fake_request(self, method, url, timeout=None, **kwargs):
        calls.append(url)
        if len(calls) < 3:
            return FakeResponse(503)
        return FakeResponse(200)

    monkeypatch.setattr(requests.Session, "request", fake_request)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    client = LangfuseRestClient(
        "http://lf.example.com", "pk", "sk", retry_delay_seconds=0
    )
    response = client.request("POST", "/scores", json={})
    assert response.status_code == 200
    assert len(calls) == 3
    assert calls[0] == "http://lf.example.com/api/public/scores"
