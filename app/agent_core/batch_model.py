"""Batch planning, adaptive budgets and resource-slot accounting.

This module deliberately does not start Jianying or perform model/native work.
It owns the durable contract used by the workbench and keeps batch planning
separate from the existing single-job workflow.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import shutil
from typing import Iterable


MAX_BATCH_OUTPUTS = 15
# Three seconds makes the boundaries between independently reviewable outputs
# visible in Jianying.  Callers may still provide an explicit gap, but every
# batch entry point uses this value when no override is supplied.
DEFAULT_GAP_US = 3_000_000
FRAME_US_30 = 1_000_000 // 30

TERMINAL_CHILD_STATES = {
    "READY_FOR_REVIEW", "COMPLETED", "FAILED", "CANCELLED",
    "WAITING_FOR_INPUT", "BLOCKED_RESOURCES",
}


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def frame_align(value_us: int, fps: int) -> int:
    if type(value_us) is not int or value_us < 0:
        raise ValueError("time must be a nonnegative integer")
    if type(fps) is not int or not 1 <= fps <= 120:
        raise ValueError("fps must be an integer in 1..120")
    return (value_us * fps // 1_000_000) * 1_000_000 // fps


def _positive_int(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def validate_batch_spec(spec: dict) -> dict:
    """Validate and normalize a batch spec without touching the filesystem."""
    if not isinstance(spec, dict):
        raise ValueError("batch spec must be an object")
    outputs = spec.get("outputs")
    if not isinstance(outputs, list) or not 1 <= len(outputs) <= MAX_BATCH_OUTPUTS:
        raise ValueError(f"batch outputs must contain 1..{MAX_BATCH_OUTPUTS} items")
    ids = []
    normalized = []
    for index, row in enumerate(outputs, 1):
        if not isinstance(row, dict):
            raise ValueError(f"output {index} must be an object")
        child_id = row.get("output_id", f"output_{index:02d}")
        if not isinstance(child_id, str) or not child_id.strip() or child_id in ids:
            raise ValueError("output_id values must be unique nonempty strings")
        instruction = row.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError(f"output {index} needs an instruction")
        assets = row.get("asset_ids")
        if not isinstance(assets, list) or not assets or len(set(assets)) != len(assets):
            raise ValueError(f"output {index} needs distinct asset_ids")
        fps = row.get("fps", spec.get("fps", 30))
        _positive_int(fps, f"output {index}.fps")
        if fps > 120:
            raise ValueError(f"output {index}.fps exceeds 120")
        normalized.append({**row, "output_id": child_id, "instruction": instruction.strip(),
                           "asset_ids": list(assets), "fps": fps})
        ids.append(child_id)
    fps = spec.get("fps", 30)
    _positive_int(fps, "fps")
    gap_us = spec.get("gap_us", DEFAULT_GAP_US)
    if type(gap_us) is not int or gap_us < 0:
        raise ValueError("gap_us must be a nonnegative integer")
    gap_us = frame_align(gap_us, fps)
    return {
        "schema_version": "1.0",
        "batch_schema": "jianying-agent-batch-v1",
        "outputs": normalized,
        "fps": fps,
        "gap_us": gap_us,
        "quality": spec.get("quality", "standard"),
        "created_at": spec.get("created_at", stamp()),
    }


def workload_features(*, duration_us=0, source_count=1, shot_count=1,
                       narration_sentences=0, caption_count=0,
                       effect_count=0, output_count=1, export_count=0,
                       width=1080, height=1920, fps=30, retries=0):
    """Return explicit features used by planning and reports.

    Coefficients are intentionally simple and monotonic. They are planning
    estimates, never a reason to reduce requested quality.
    """
    values = {
        "duration_s": max(0.0, float(duration_us) / 1_000_000),
        "source_count": max(1, int(source_count)),
        "shot_count": max(1, int(shot_count)),
        "narration_sentences": max(0, int(narration_sentences)),
        "caption_count": max(0, int(caption_count)),
        "effect_count": max(0, int(effect_count)),
        "output_count": max(1, int(output_count)),
        "pixels_m": max(0.1, float(width) * float(height) / 1_000_000),
        "fps": max(1, int(fps)),
        "export_count": max(0, int(export_count)),
        "retries": max(0, int(retries)),
    }
    # One comparable unit is roughly a simple short-output job. The values are
    # estimates only; measured stage times can replace them in a later profile.
    values["work_units"] = round(
        1.0 + values["duration_s"] / 15.0 * 0.8
        + values["source_count"] * 0.35
        + values["shot_count"] * 0.06
        + values["narration_sentences"] * 0.10
        + values["caption_count"] * 0.025
        + values["effect_count"] * 0.12
        + values["output_count"] * 0.45
        + values["pixels_m"] * 0.12
        + values["fps"] / 30.0 * 0.08
        + values["export_count"] * 0.70
        + values["retries"] * 0.50, 3)
    return values


def estimate_timing(features: dict, *, cold_start=False, authorization_wait=0,
                    native_capacity=1, model_capacity=1) -> dict:
    """Return an explainable, adaptive estimate and soft SLO.

    The ten-minute objective is represented by the reference workload, not a
    special duration branch. Complex work receives a larger budget instead of
    silently dropping effects or checks.
    """
    f = workload_features(
        duration_us=features.get('duration_us', features.get('duration_s', 0) * 1_000_000),
        source_count=features.get('source_count', 1),
        shot_count=features.get('shot_count', 1),
        narration_sentences=features.get('narration_sentences', 0),
        caption_count=features.get('caption_count', 0),
        effect_count=features.get('effect_count', 0),
        output_count=features.get('output_count', 1),
        export_count=features.get('export_count', 0),
        width=features.get('width', 1080), height=features.get('height', 1920),
        fps=features.get('fps', 30), retries=features.get('retries', 0))
    base = 95.0 + f["work_units"] * 185.0
    if cold_start:
        base += 70.0
    analysis = 35.0 + f["duration_s"] * 2.0 + f["source_count"] * 12.0 + f["shot_count"] * 1.5
    model = 20.0 + f["narration_sentences"] * 1.4 + f["effect_count"] * 1.0
    native = 30.0 + f["duration_s"] * 2.4 + f["effect_count"] * 2.2
    export = f["export_count"] * (35.0 + f["duration_s"] * 1.7)
    queue = max(0.0, (f["output_count"] - max(1, native_capacity)) * 18.0)
    queue += max(0.0, f["output_count"] - max(1, model_capacity)) * 5.0
    total = base + analysis + model + native + export + queue + max(0, authorization_wait)
    if f["output_count"] == 1 and f["duration_s"] <= 15 and f["effect_count"] <= 4:
        target = 600.0  # reference SLO; not a separate execution path
    else:
        target = max(600.0, total * 1.25)
    reasons = []
    if cold_start:
        reasons.append("首次启动或冷缓存")
    if authorization_wait:
        reasons.append("等待授权")
    if f["source_count"] > 3:
        reasons.append("素材数量较多")
    if f["effect_count"] > 8:
        reasons.append("原生包装元素较多")
    if f["output_count"] > 1:
        reasons.append("批量输出排队")
    return {"schema_version": "1.0", "work_units": f["work_units"],
            "features": f, "estimated_seconds": round(total, 1),
            "soft_budget_seconds": round(target, 1), "reasons": reasons,
            "quality_preserved": True, "generated_at": stamp()}


@dataclass(frozen=True)
class SlotLease:
    kind: str
    token: str
    capacity: int


class ResourceSlots:
    """Small in-process slot manager used by the scheduler and tests."""

    DEFAULTS = {"model": 2, "analysis": 2, "native": 1, "tts": 1,
                "registration": 1, "export": 1}

    def __init__(self, capacities=None):
        self.capacities = {**self.DEFAULTS, **(capacities or {})}
        self.used = {key: 0 for key in self.capacities}
        self._tokens = {}

    def acquire(self, kind: str) -> SlotLease | None:
        if kind not in self.capacities:
            raise ValueError("unknown resource slot: " + str(kind))
        if self.used[kind] >= self.capacities[kind]:
            return None
        token = f"{kind}-{os.getpid()}-{id(self)}-{self.used[kind]+1}"
        self.used[kind] += 1
        self._tokens[token] = kind
        return SlotLease(kind, token, self.capacities[kind])

    def release(self, lease: SlotLease):
        if not isinstance(lease, SlotLease) or lease.token not in self._tokens:
            raise ValueError("unknown or already released slot lease")
        kind = self._tokens.pop(lease.token)
        self.used[kind] = max(0, self.used[kind] - 1)

    def snapshot(self):
        return {"capacities": dict(self.capacities), "used": dict(self.used),
                "available": {k: self.capacities[k] - self.used[k] for k in self.capacities}}


def batch_counts(child_states: Iterable[str]) -> dict:
    counts = {"success": 0, "failed": 0, "processing": 0,
              "waiting_manual": 0, "retryable": 0}
    for state in child_states:
        if state in ("READY_FOR_REVIEW", "COMPLETED"):
            counts["success"] += 1
        elif state in ("FAILED", "CANCELLED"):
            counts["failed"] += 1
            if state == "FAILED":
                counts["retryable"] += 1
        elif state in ("WAITING_FOR_INPUT", "BLOCKED_RESOURCES"):
            counts["waiting_manual"] += 1
            if state == "BLOCKED_RESOURCES":
                counts["retryable"] += 1
        else:
            counts["processing"] += 1
    return counts


def batch_state(child_states: Iterable[str]) -> str:
    states = list(child_states)
    if not states:
        return "EMPTY"
    counts = batch_counts(states)
    if counts["processing"]:
        return "PROCESSING"
    if counts["failed"]:
        return "PARTIAL_FAILURE" if counts["success"] else "FAILED"
    if counts["waiting_manual"]:
        return "WAITING_FOR_INPUT"
    return "READY_FOR_REVIEW"
