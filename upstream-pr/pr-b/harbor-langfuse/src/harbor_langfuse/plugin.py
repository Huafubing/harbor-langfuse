"""Langfuse job plugin for Harbor.

Mirrors the ``LangSmithPlugin`` architecture:

- Dataset sync: Harbor tasks → a Langfuse dataset (items upserted by stable id)
- Experiment mapping: a Harbor job becomes one Langfuse experiment; every
  trial's ATIF trajectory is converted to OTel spans (via ``harbor-atif2otel``)
  and enriched with ``langfuse.*`` attributes so Langfuse groups the traces
  under the experiment.
- Score backfill: ``TrialResult.verifier_result.rewards`` → Scores API; trials
  that ended with an exception get a boolean ``harbor_error`` score.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from harbor.job import Job
from harbor.models.job.plugin import BaseJobPlugin
from harbor.models.job.result import JobResult
from harbor.trial.hooks import TrialEvent, TrialHookEvent

from harbor_langfuse.api import LangfuseRestClient
from harbor_langfuse import enrich as enrich_module
from harbor_langfuse.scores import (
    build_exception_score,
    build_score_items,
    post_scores,
)

logger = logging.getLogger(__name__)

_RETRYABLE = frozenset({408, 429, 500, 502, 503, 504})


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class LangfusePlugin(BaseJobPlugin):
    """Sync Harbor jobs to Langfuse.

    Args:
        host: Langfuse base URL (env: ``LANGFUSE_HOST``).
        public_key: Langfuse public key ``pk-lf-...`` (env: ``LANGFUSE_PUBLIC_KEY``).
        secret_key: Langfuse secret key ``sk-lf-...`` (env: ``LANGFUSE_SECRET_KEY``).
        dataset_name: sync Harbor tasks into this dataset (env:
            ``HARBOR_LANGFUSE_DATASET``).
        experiment_name: experiment label shown in Langfuse (env:
            ``HARBOR_LANGFUSE_EXPERIMENT``; default ``{job_name}-{job_id[:8]}``).
        release: optional release tag forwarded to traces (env:
            ``HARBOR_LANGFUSE_RELEASE``).
        sync_dataset: create/update the dataset and its items (default True).
        export_traces: convert each trial's ATIF trajectory to OTel spans and
            upload them to Langfuse (default True; requires the
            ``harbor-atif2otel`` package).
        fail_fast: re-raise plugin errors instead of logging them (default
            False).
    """

    def __init__(
        self,
        *,
        host: str | None = None,
        public_key: str | None = None,
        secret_key: str | None = None,
        dataset_name: str | None = None,
        experiment_name: str | None = None,
        release: str | None = None,
        sync_dataset: bool | None = None,
        export_traces: bool | None = None,
        fail_fast: bool | None = None,
        request_timeout: float | None = None,
        request_retries: int | None = None,
        request_retry_delay: float | None = None,
    ):
        super().__init__()
        self.host = host or os.getenv("LANGFUSE_HOST")
        self.public_key = public_key or os.getenv("LANGFUSE_PUBLIC_KEY")
        self.secret_key = secret_key or os.getenv("LANGFUSE_SECRET_KEY")
        self.dataset_name = dataset_name or os.getenv("HARBOR_LANGFUSE_DATASET")
        self.experiment_name = experiment_name or os.getenv(
            "HARBOR_LANGFUSE_EXPERIMENT"
        )
        self.release = release or os.getenv("HARBOR_LANGFUSE_RELEASE") or None
        self.sync_dataset = (
            _env_bool("HARBOR_LANGFUSE_SYNC_DATASET", default=True)
            if sync_dataset is None
            else sync_dataset
        )
        self.export_traces = (
            _env_bool("HARBOR_LANGFUSE_EXPORT_TRACES", default=True)
            if export_traces is None
            else export_traces
        )
        self.fail_fast = (
            _env_bool("HARBOR_LANGFUSE_FAIL_FAST", default=False)
            if fail_fast is None
            else fail_fast
        )
        self.request_timeout = float(
            os.getenv("HARBOR_LANGFUSE_REQUEST_TIMEOUT", "30")
            if request_timeout is None
            else request_timeout
        )
        self.request_retries = int(
            os.getenv("HARBOR_LANGFUSE_REQUEST_RETRIES", "3")
            if request_retries is None
            else request_retries
        )
        self.request_retry_delay = float(
            os.getenv("HARBOR_LANGFUSE_REQUEST_RETRY_DELAY", "1")
            if request_retry_delay is None
            else request_retry_delay
        )

        self._client: LangfuseRestClient | None = None
        self._experiment_id: str | None = None
        self._experiment_name: str | None = None
        self._dataset_id: str | None = None
        self._job_dir: Any = None
        self._job_name: str = ""
        self._convert_trajectory = None
        self._trajectory_trace_seed = None
        self._sha256_trace_id = None
        self._sha256_span_id = None
        self._trajectory_span_seed = None
        self._uploader = None
        self._trials_exported = 0
        self._scores_ok = 0
        self._scores_failed = 0

    # ---------- job lifecycle ----------

    async def on_job_start(self, job: Job) -> None:
        await asyncio.to_thread(self._setup, job)
        if self._client is not None:
            job.on_trial_ended(self._handle_event)
            job.on_trial_cancelled(self._handle_event)

    async def on_job_end(self, job_result: JobResult) -> None:
        if self._client is None:
            return
        logger.info(
            "LangfusePlugin job %s complete: %d trials exported, "
            "%d/%d scores written (experiment=%s)",
            self._job_name,
            self._trials_exported,
            self._scores_ok,
            self._scores_ok + self._scores_failed,
            self._experiment_name,
        )

    # ---------- setup ----------

    def _setup(self, job: Job) -> None:
        if not self.public_key or not self.secret_key:
            raise RuntimeError(
                "LangfusePlugin requires LANGFUSE_PUBLIC_KEY and "
                "LANGFUSE_SECRET_KEY"
            )
        if not self.host:
            raise RuntimeError("LangfusePlugin requires LANGFUSE_HOST")

        self._client = LangfuseRestClient(
            self.host,
            self.public_key,
            self.secret_key,
            timeout_seconds=self.request_timeout,
            max_retries=self.request_retries,
            retry_delay_seconds=self.request_retry_delay,
        )
        self._job_dir = job.job_dir
        self._job_name = job.config.job_name
        self._experiment_name = self.experiment_name or (
            f"{job.config.job_name}-{str(job.id)[:8]}"
        )
        # Stable experiment id: the same dataset+experiment name always maps to
        # the same correlation id so repeated jobs group into one experiment.
        self._experiment_id = str(
            uuid5(
                NAMESPACE_URL,
                f"harbor:{self.host}:{self._experiment_name}",
            )
        )

        if self.sync_dataset:
            self._dataset_id = self._get_or_create_dataset(job)
            self._sync_examples(job)

        if self.export_traces:
            self._init_converter()

    def _init_converter(self) -> None:
        try:
            from harbor_atif2otel.convert import convert_trajectory
            from harbor_atif2otel.ids import (
                sha256_span_id,
                sha256_trace_id,
                trajectory_span_seed,
                trajectory_trace_seed,
            )
            from harbor_atif2otel.uploaders.langfuse import LangfuseUploader
        except ImportError:
            logger.warning(
                "LangfusePlugin: export_traces=True requires the "
                "harbor-atif2otel package; falling back to scores-only mode"
            )
            self.export_traces = False
            return
        self._convert_trajectory = convert_trajectory
        self._trajectory_trace_seed = trajectory_trace_seed
        self._trajectory_span_seed = trajectory_span_seed
        self._sha256_trace_id = sha256_trace_id
        self._sha256_span_id = sha256_span_id
        self._uploader = LangfuseUploader(
            host=self.host or "",
            public_key=self.public_key or "",
            secret_key=self.secret_key or "",
        )

    # ---------- dataset sync ----------

    def _get_or_create_dataset(self, job: Job) -> str | None:
        dataset_name = self.dataset_name or f"harbor-{job.config.job_name}"
        try:
            response = self._client.request(
                "GET",
                f"/datasets/{dataset_name}",
                ok_statuses=(200, 404),
            )
            if response.status_code == 200:
                data = response.json()
                dataset_id = data.get("id")
                if dataset_id:
                    logger.info(
                        "LangfusePlugin: reusing dataset %s (%s)",
                        dataset_name,
                        dataset_id,
                    )
                    return dataset_id
        except Exception:
            if self.fail_fast:
                raise
            logger.warning(
                "LangfusePlugin: dataset lookup failed for %s", dataset_name,
                exc_info=True,
            )

        response = self._client.request(
            "POST",
            "/datasets",
            ok_statuses=(200, 201, 409),
            json={
                "name": dataset_name,
                "description": f"Harbor dataset synced from job {job.config.job_name}",
                "metadata": {"source": "harbor"},
            },
        )
        if response.status_code == 409:
            lookup = self._client.request(
                "GET", f"/datasets/{dataset_name}", ok_statuses=(200, 404)
            )
            if lookup.status_code == 200:
                return lookup.json().get("id")
        try:
            return response.json().get("id")
        except Exception:
            return None

    def _sync_examples(self, job: Job) -> None:
        if self._dataset_id is None:
            return
        for task_config in getattr(job, "_task_configs", []) or []:
            try:
                task_id = task_config.get_task_id()
                full_name = task_id.get_name()
                task_name = full_name.split("/")[-1]
                item_id = str(
                    uuid5(NAMESPACE_URL, f"{self._dataset_id}:{task_name}")
                )
                instruction = self._read_instruction(task_config)
                payload: dict[str, Any] = {
                    "id": item_id,
                    "datasetName": self.dataset_name
                    or f"harbor-{job.config.job_name}",
                    "input": {
                        "task_name": task_name,
                        "instruction": instruction,
                        "task_id": task_id.model_dump(mode="json"),
                    },
                    "metadata": {"source": "harbor"},
                }
                self._client.request(
                    "POST", "/dataset-items", ok_statuses=(200, 201), json=payload
                )
            except Exception:
                if self.fail_fast:
                    raise
                logger.warning(
                    "LangfusePlugin: failed to sync task item", exc_info=True
                )

    @staticmethod
    def _read_instruction(task_config: Any) -> str | None:
        for attr in ("path", "task_dir", "dir"):
            path = getattr(task_config, attr, None)
            if path is None:
                continue
            try:
                return (path / "instruction.md").read_text(encoding="utf-8")
            except OSError:
                continue
        return None

    # ---------- trial handling ----------

    async def _handle_event(self, event: TrialHookEvent) -> None:
        try:
            await asyncio.to_thread(self._handle_event_sync, event)
        except Exception:
            if self.fail_fast:
                raise
            logger.warning(
                "LangfusePlugin: trial handling failed for %s",
                event.config.trial_name,
                exc_info=True,
            )

    def _handle_event_sync(self, event: TrialHookEvent) -> None:
        if event.event in {TrialEvent.END, TrialEvent.CANCEL}:
            self._finish_trial(event)

    def _finish_trial(self, event: TrialHookEvent) -> None:
        result = event.result
        trace_id = None
        root_span_id = None

        if self.export_traces and self._convert_trajectory is not None:
            trace_id, root_span_id = self._export_trial(event)

        rewards: dict[str, Any] = {}
        errored = False
        error_message = None
        if result is not None:
            verifier_result = result.verifier_result
            if verifier_result is not None:
                rewards = dict(verifier_result.rewards or {})
            if result.exception_info is not None:
                errored = True
                error_message = (
                    f"{result.exception_info.exception_type}: "
                    f"{result.exception_info.exception_message}"
                )

        id_scope = f"{self._experiment_id}:{event.config.trial_name}"
        items = build_score_items(
            rewards,
            trace_id=trace_id,
            observation_id=root_span_id,
            session_id=self._experiment_id,
            id_scope=id_scope,
        )
        if errored and not items:
            items.append(
                build_exception_score(
                    trace_id=trace_id,
                    observation_id=root_span_id,
                    session_id=self._experiment_id,
                    id_scope=id_scope,
                    message=error_message,
                )
            )
        ok, failed = post_scores(self._client, items)
        self._scores_ok += ok
        self._scores_failed += failed

    def _export_trial(self, event: TrialHookEvent) -> tuple[str | None, str | None]:
        if self._job_dir is None:
            return None, None
        trial_dir = self._job_dir / event.config.trial_name
        traj_path = trial_dir / "agent" / "trajectory.json"
        try:
            trajectory = json.loads(traj_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "LangfusePlugin: trajectory missing/unreadable for %s",
                event.config.trial_name,
            )
            return None, None

        result = event.result
        info = getattr(result, "agent_info", None) if result is not None else None
        agent_name = (
            info.name
            if info is not None
            else (event.config.agent.name or event.config.agent.import_path)
        )
        model_name = (
            info.model_info.name
            if info is not None and info.model_info is not None
            else None
        ) or event.config.agent.model_name or "unknown"

        resource_spans = self._convert_trajectory(trajectory)
        seed = self._trajectory_trace_seed(trajectory)
        trace_id = self._sha256_trace_id(seed).hex()
        root_span_id = self._sha256_span_id(
            self._trajectory_span_seed(trajectory, trace_id) + ":root"
        ).hex()

        enrich_module.enrich_resource_spans(
            resource_spans,
            session_id=trajectory.get("session_id"),
            trace_name=event.config.trial_name,
            tags=["harbor", agent_name, model_name],
            experiment_id=self._experiment_id,
            experiment_name=self._experiment_name,
            dataset_id=self._dataset_id,
            item_id=event.task_name,
            user_id=agent_name,
            release=self.release,
        )
        self._uploader.upload(resource_spans)
        self._trials_exported += 1
        return trace_id, root_span_id
