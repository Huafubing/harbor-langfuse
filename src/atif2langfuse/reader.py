# -*- coding: utf-8 -*-
"""读取层：发现 Trial 目录，解析 ATIF 轨迹与 Verifier 判分。

优先复用 Harbor 主线 harbor.utils.traces_utils 的目录发现逻辑；
未安装 Harbor 时回退到内置兼容实现。判分读取遵循 Harbor 约定：
先 verifier/reward.txt，缺省回退 verifier/reward.json；
分维度得分读 verifier/reward-details.json（Reward Kit）。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

try:
    from harbor.utils.traces_utils import (  # type: ignore
        iter_trial_dirs as _harbor_iter_trial_dirs,
    )
    _HAS_HARBOR = True
except Exception:
    _HAS_HARBOR = False


@dataclass
class StepRecord:
    step_id: int
    source: str
    timestamp: Optional[str]
    model_name: Optional[str]
    message: str
    reasoning_content: Optional[str]
    tool_calls: List[Dict[str, Any]]
    observation_text: str
    metrics: Dict[str, Any]


@dataclass
class TrialRecord:
    trial_dir: Path
    task_id: str
    agent_name: str
    model_name: str
    attempt: str
    run_id: str
    session_id: str
    task_prompt: str
    final_message: str
    steps: List[StepRecord]
    final_metrics: Dict[str, Any]
    subagent_trajectories: List[Dict[str, Any]]
    reward: Optional[float]
    reward_details: Dict[str, float]
    phenotypes: List[str]


# ---------- 目录发现 ----------

def iter_trial_dirs(root: Union[str, Path]) -> Iterator[Path]:
    root = Path(root)
    if _HAS_HARBOR:
        yield from _harbor_iter_trial_dirs(root, recursive=True)
        return
    if _looks_like_trial(root):
        yield root
        return
    for p in sorted(root.rglob("*")):
        if p.is_dir() and _looks_like_trial(p):
            yield p


def _looks_like_trial(p: Path) -> bool:
    return (p / "agent" / "trajectory.json").exists()


# ---------- 元数据 ----------

def _parse_dir_name(name: str) -> Dict[str, str]:
    """回退方案：Harbor/TB 约定的 trial 目录名 task__agent__model__attempt。"""
    parts = name.split("__")
    if len(parts) >= 4:
        return {"task_id": parts[0], "agent_name": parts[1],
                "model_name": "__".join(parts[2:-1]), "attempt": parts[-1]}
    if len(parts) == 3:
        return {"task_id": parts[0], "agent_name": parts[1],
                "model_name": parts[2], "attempt": "1"}
    return {"task_id": name, "agent_name": "unknown",
            "model_name": "unknown", "attempt": "1"}


def _meta_from_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """容错读取 result.json：不同 Harbor 版本字段名略有差异。"""
    agent = result.get("agent_name") or (result.get("agent") or {}).get("name")
    model = result.get("model_name") or (result.get("agent") or {}).get("model_name")
    task = (result.get("task_name") or result.get("task_id")
            or (result.get("task") or {}).get("name"))
    run_id = result.get("run_id") or result.get("job_id")
    res = (result.get("result") or result.get("trial_result")
           or result.get("verifier_result") or {})
    reward = res.get("reward") if isinstance(res, dict) else None
    if reward is None:
        reward = result.get("reward")
    return {"agent_name": agent, "model_name": model, "task_id": task,
            "run_id": run_id, "reward": reward}


# ---------- 内容兼容 ----------

def _text_of(content: Any) -> str:
    """兼容 ATIF v1.6+：message/content 可为字符串或 ContentPart 列表。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict) and isinstance(c.get("text"), str):
                parts.append(c["text"])
        return "\n".join(parts)
    return str(content)


# ---------- 判分 ----------

def load_reward(trial_dir: Path) -> Tuple[Optional[float], Dict[str, float]]:
    vdir = trial_dir / "verifier"
    txt = vdir / "reward.txt"
    if txt.exists():
        try:
            return float(txt.read_text(encoding="utf-8").strip()), {}
        except ValueError:
            pass
    rj = vdir / "reward.json"
    if rj.exists():
        try:
            data = json.loads(rj.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None, {}
        if isinstance(data, (int, float)) and not isinstance(data, bool):
            return float(data), {}
        if isinstance(data, dict):
            reward = data.get("reward", data.get("score"))
            details: Dict[str, float] = {}
            for k, v in (data.get("details") or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    details[str(k)] = float(v)
            try:
                reward_f = float(reward) if reward is not None else None
            except (TypeError, ValueError):
                reward_f = None
            return reward_f, details
    return None, {}


def load_reward_details(trial_dir: Path) -> Dict[str, float]:
    """Reward Kit 的 reward-details.json：逐判据得分，结构宽容解析。"""
    p = trial_dir / "verifier" / "reward-details.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    out: Dict[str, float] = {}

    def eat(obj: Any) -> None:
        if isinstance(obj, dict):
            if isinstance(obj.get("criteria"), list):
                for c in obj["criteria"]:
                    eat(c)
                return
            name = obj.get("name") or obj.get("criterion") or obj.get("id")
            score = obj.get("score", obj.get("value"))
            if name is not None and isinstance(score, (int, float)) \
                    and not isinstance(score, bool):
                out[str(name)] = float(score)
            else:
                for k, v in obj.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        out[str(k)] = float(v)
        elif isinstance(obj, list):
            for c in obj:
                eat(c)

    eat(data)
    return out


# ---------- Trial 解析 ----------

def load_trial(trial_dir: Union[str, Path], run_id_hint: str = "") -> TrialRecord:
    trial_dir = Path(trial_dir)
    traj_path = trial_dir / "agent" / "trajectory.json"
    traj = json.loads(traj_path.read_text(encoding="utf-8"))

    result: Dict[str, Any] = {}
    rp = trial_dir / "result.json"
    if rp.exists():
        try:
            result = json.loads(rp.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            result = {}

    base = _parse_dir_name(trial_dir.name)
    from_result = _meta_from_result(result)
    task_id = from_result.get("task_id") or base["task_id"]
    agent_name = from_result.get("agent_name") or base["agent_name"]
    model_name = from_result.get("model_name") or base["model_name"]
    run_id = from_result.get("run_id") or run_id_hint \
        or (trial_dir.parent.name if trial_dir.parent.name else "unknown-run")

    agent_cfg = traj.get("agent") or {}
    if agent_name == "unknown":
        agent_name = agent_cfg.get("name", agent_name)
    if model_name == "unknown":
        model_name = agent_cfg.get("model_name", model_name)

    steps: List[StepRecord] = []
    for raw in traj.get("steps") or []:
        obs = raw.get("observation") or {}
        obs_text = "\n".join(
            _text_of(r.get("content")) for r in (obs.get("results") or [])
        )
        steps.append(StepRecord(
            step_id=int(raw.get("step_id") or 0),
            source=str(raw.get("source") or ""),
            timestamp=raw.get("timestamp"),
            model_name=raw.get("model_name") or agent_cfg.get("model_name"),
            message=_text_of(raw.get("message")),
            reasoning_content=raw.get("reasoning_content"),
            tool_calls=list(raw.get("tool_calls") or []),
            observation_text=obs_text,
            metrics=dict(raw.get("metrics") or {}),
        ))

    task_prompt = next((s.message for s in steps
                        if s.source == "user" and s.message), "") \
        or next((s.message for s in steps
                 if s.source == "system" and s.message), "")
    final_message = next((s.message for s in reversed(steps)
                          if s.source == "agent" and s.message), "")

    reward, details = load_reward(trial_dir)
    if reward is None and from_result.get("reward") is not None:
        try:
            reward = float(from_result["reward"])
        except (TypeError, ValueError):
            reward = None
    if not details:
        details = load_reward_details(trial_dir)

    extra = traj.get("extra") or {}
    phenotypes = [str(t) for t in
                  (extra.get("phenotypes") or result.get("phenotypes") or [])]

    session_id = str(traj.get("session_id") or f"{run_id}:{trial_dir.name}")

    return TrialRecord(
        trial_dir=trial_dir,
        task_id=str(task_id),
        agent_name=str(agent_name),
        model_name=str(model_name),
        attempt=str(base["attempt"]),
        run_id=str(run_id),
        session_id=session_id,
        task_prompt=task_prompt,
        final_message=final_message,
        steps=steps,
        final_metrics=dict(traj.get("final_metrics") or {}),
        subagent_trajectories=list(traj.get("subagent_trajectories") or []),
        reward=reward,
        reward_details=details,
        phenotypes=phenotypes,
    )


def load_trials(root: Union[str, Path], trial_filter: str = "all",
                limit: Optional[int] = None, run_id_hint: str = ""
                ) -> Tuple[List[TrialRecord], int]:
    """发现并解析全部 Trial；返回 (记录列表, 跳过数)。"""
    records: List[TrialRecord] = []
    skipped = 0
    for d in iter_trial_dirs(root):
        try:
            rec = load_trial(d, run_id_hint=run_id_hint)
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            print(f"[skip] {d}: {exc}", file=sys.stderr)
            continue
        if trial_filter == "success" and not (rec.reward is not None and rec.reward > 0):
            continue
        if trial_filter == "failure" and not (rec.reward is None or rec.reward <= 0):
            continue
        records.append(rec)
        if limit and len(records) >= limit:
            break
    return records, skipped
