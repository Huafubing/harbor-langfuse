"""Test configuration.

On upstream CI (Python 3.12 + workspace install) the real ``harbor`` package
is available. For local runs without a harbor install (e.g. Python 3.10),
minimal stubs of the harbor interfaces used by the plugin are injected so the
plugin module can be imported and its logic exercised.
"""

import sys
import types
from abc import ABC, abstractmethod
from enum import Enum

try:
    import harbor.trial.hooks  # noqa: F401
    _HAS_HARBOR = True
except Exception:
    _HAS_HARBOR = False

if not _HAS_HARBOR:
    harbor = types.ModuleType("harbor")
    sys.modules["harbor"] = harbor

    job_mod = types.ModuleType("harbor.job")

    class Job:  # minimal stand-in; tests subclass/patch as needed
        pass

    job_mod.Job = Job
    sys.modules["harbor.job"] = job_mod

    models_mod = types.ModuleType("harbor.models")
    sys.modules["harbor.models"] = models_mod
    models_job_mod = types.ModuleType("harbor.models.job")
    sys.modules["harbor.models.job"] = models_job_mod
    plugin_mod = types.ModuleType("harbor.models.job.plugin")

    class BaseJobPlugin(ABC):
        def __init__(self, **kwargs):
            pass

        @abstractmethod
        async def on_job_start(self, job):
            ...

        @abstractmethod
        async def on_job_end(self, job_result):
            ...

    plugin_mod.BaseJobPlugin = BaseJobPlugin
    sys.modules["harbor.models.job.plugin"] = plugin_mod

    hooks_mod = types.ModuleType("harbor.trial.hooks")

    class TrialEvent(Enum):
        START = "start"
        ENVIRONMENT_START = "environment-start"
        AGENT_START = "agent-start"
        AGENT_END = "agent-end"
        VERIFICATION_START = "verification-start"
        END = "end"
        CANCEL = "cancel"

    class TrialHookEvent:
        """Plain stand-in matching the real event's attribute surface."""

        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

        @property
        def trial_name(self):
            return self.config.trial_name

        @property
        def trial_id(self):
            return getattr(self, "_trial_id", None)

    hooks_mod.TrialEvent = TrialEvent
    hooks_mod.TrialHookEvent = TrialHookEvent
    sys.modules["harbor.trial"] = types.ModuleType("harbor.trial")
    sys.modules["harbor.trial"].hooks = hooks_mod
    sys.modules["harbor.trial.hooks"] = hooks_mod

    result_mod = types.ModuleType("harbor.models.job.result")

    class JobResult:
        pass

    result_mod.JobResult = JobResult
    sys.modules["harbor.models.job.result"] = result_mod
