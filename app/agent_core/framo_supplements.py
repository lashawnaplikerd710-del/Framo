"""Small, local supplements inspired by VideoHighlighter, video-evidence and OpenClipper.

This module deliberately stays provider- and UI-independent.  Framo keeps the
authority over task rules, copy, narration and rendering; this layer adds
auditable evidence IDs, deterministic highlight signals and optional subject
layout hints.  Detector-specific fields are accepted when a worker provides
them, but the absence of a detector never becomes a fabricated observation.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
from typing import Any, Iterable


VERSION = "framo_supplements_v1"
SCHEMA_VERSION = "1.0"


def _text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value or "")


def _safe_id(value: Any) -> str:
    value = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(value or ""))
    return value[:80] or "unknown"


def evidence_id(asset_id: str, time_us: int, kind: str = "frame", digest: str = "") -> str:
    suffix = (digest or "")[:10]
    return f"{_safe_id(asset_id)}_{kind}_{int(time_us)}_{suffix}".rstrip("_")


def _observation_rows(observations: Any) -> list[dict[str, Any]]:
    """Flatten direct-vision reports while retaining only evidence-shaped rows."""
    if isinstance(observations, dict):
        observations = observations.get("frames", [])
    rows: list[dict[str, Any]] = []
    for report in observations or []:
        if isinstance(report, dict) and isinstance(report.get("frames"), list):
            rows.extend(row for row in report["frames"] if isinstance(row, dict))
        elif isinstance(report, dict):
            rows.append(report)
    return rows


def _normalise_box(raw: Any) -> dict[str, float] | None:
    """Return x/y/w/h in 0..1 from common detector box shapes."""
    if isinstance(raw, dict):
        if all(key in raw for key in ("x", "y", "w", "h")):
            values = [raw[key] for key in ("x", "y", "w", "h")]
        elif all(key in raw for key in ("x0", "y0", "x1", "y1")):
            values = [raw["x0"], raw["y0"], raw["x1"] - raw["x0"], raw["y1"] - raw["y0"]]
        elif all(key in raw for key in ("left", "top", "right", "bottom")):
            values = [raw["left"], raw["top"], raw["right"] - raw["left"], raw["bottom"] - raw["top"]]
        else:
            return None
    elif isinstance(raw, (list, tuple)) and len(raw) == 4:
        values = list(raw)
    else:
        return None
    try:
        values = [float(value) for value in values]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    # Detector workers may return pixels.  If they do, the report should also
    # carry frame_width/frame_height so the conversion is auditable.
    if max(abs(value) for value in values) > 1.0:
        if not isinstance(raw, dict):
            return None
        width, height = raw.get("frame_width"), raw.get("frame_height")
        if not width or not height:
            return None
        values = [values[0] / float(width), values[1] / float(height),
                  values[2] / float(width), values[3] / float(height)]
    x, y, w, h = values
    if w <= 0 or h <= 0:
        return None
    x = min(1.0, max(0.0, x)); y = min(1.0, max(0.0, y))
    w = min(1.0 - x, max(0.0, w)); h = min(1.0 - y, max(0.0, h))
    if w <= 0 or h <= 0:
        return None
    return {"x": round(x, 6), "y": round(y, 6), "w": round(w, 6), "h": round(h, 6)}


def observation_box(row: dict[str, Any]) -> dict[str, float] | None:
    for key in ("subject_bbox", "bounding_box", "bbox", "box"):
        box = _normalise_box(row.get(key))
        if box:
            return box
    return None


def _frame_index(frames: Iterable[dict[str, Any]] | None) -> dict[tuple[str, int], str]:
    result: dict[tuple[str, int], str] = {}
    for frame in frames or []:
        if not isinstance(frame, dict) or type(frame.get("requested_us")) is not int:
            continue
        aid = str(frame.get("asset_id") or "unknown")
        result[(aid, frame["requested_us"])] = evidence_id(
            aid, frame["requested_us"], "frame", str(frame.get("sha256") or ""))
    return result


def score_span(start_us: int, end_us: int, *, asset_id: str = "", observations: Any = None,
               frames: Iterable[dict[str, Any]] | None = None, text: str = "") -> dict[str, Any]:
    """Compute a conservative, deterministic highlight score.

    This is intentionally a signal combiner, not a claim that motion/audio
    detectors ran.  Missing signals are listed explicitly in the result.
    """
    rows = [row for row in _observation_rows(observations)
            if (not asset_id or row.get("asset_id") in (None, asset_id))
            and type(row.get("requested_us")) is int
            and start_us <= row["requested_us"] < end_us]
    frame_rows = [row for row in (frames or [])
                  if (not asset_id or row.get("asset_id") == asset_id)
                  and type(row.get("requested_us")) is int
                  and start_us <= row["requested_us"] < end_us]
    confidence_values = {"high": 1.0, "medium": 0.65, "low": 0.25}
    visual = (sum(confidence_values.get(row.get("confidence"), 0.0) for row in rows) / len(rows)
              if rows else 0.0)
    actions = sum(bool(_text(row.get("action")).strip()) for row in rows)
    action_signal = actions / len(rows) if rows else 0.0
    speech_units = sum(1 for char in str(text or "") if char.isalnum() or "\u3400" <= char <= "\u9fff")
    span_seconds = max(0.001, (end_us - start_us) / 1_000_000)
    speech_signal = min(1.0, speech_units / max(8.0, span_seconds * 3.0))
    coverage = min(1.0, len(frame_rows) / 3.0)
    score = round(0.45 * visual + 0.25 * action_signal + 0.20 * speech_signal + 0.10 * coverage, 4)
    ids = []
    frame_ids = _frame_index(frame_rows)
    for row in rows:
        aid = str(row.get("asset_id") or asset_id or "unknown")
        timestamp = int(row["requested_us"])
        ids.append(evidence_id(aid, timestamp, "visual", str(row.get("frame_sha256") or "")))
    ids.extend(frame_ids.values())
    return {
        "score": score,
        "signals": {
            "visual_confidence": round(visual, 4),
            "action_presence": round(action_signal, 4),
            "speech_density": round(speech_signal, 4),
            "sample_coverage": round(coverage, 4),
        },
        "evidence_ids": list(dict.fromkeys(ids)),
        "available_signals": ["visual_confidence", "action_presence", "speech_density", "sample_coverage"],
        "unavailable_signals": ["continuous_motion", "audio_peak", "scene_change", "tracked_objects"],
        "source": "videohighlighter_inspired_local_signal_fusion",
    }


def annotate_candidates(candidates: Iterable[dict[str, Any]], *, observations: Any = None,
                        frames: Iterable[dict[str, Any]] | None = None,
                        transcript: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    cues = (transcript or {}).get("cues", []) if isinstance(transcript, dict) else []
    result = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        start, end = candidate.get("start_us"), candidate.get("end_us")
        if type(start) is not int or type(end) is not int or end <= start:
            result.append(dict(candidate)); continue
        text = candidate.get("text") or "".join(
            str(cue.get("text") or "") for cue in cues
            if isinstance(cue, dict) and start <= cue.get("start_us", -1) < end)
        scored = score_span(start, end, asset_id=str(candidate.get("asset_id") or ""),
                            observations=observations, frames=frames, text=text)
        existing_ids = [value for value in [candidate.get("evidence_id"), *scored["evidence_ids"]]
                        if isinstance(value, str) and value]
        result.append({**candidate, "highlight_score": scored["score"],
                       "highlight_signals": scored["signals"],
                       "evidence_ids": list(dict.fromkeys(existing_ids)),
                       "highlight_score_source": scored["source"],
                       "highlight_unavailable_signals": scored["unavailable_signals"]})
    return result


def _layout_for_span(mapping: dict[str, Any], observations: Any) -> dict[str, Any]:
    start = int(mapping.get("source_start_us", 0)); end = int(mapping.get("source_end_us", start))
    aid = mapping.get("asset_id")
    rows = [row for row in _observation_rows(observations)
            if row.get("asset_id") in (None, aid)
            and type(row.get("requested_us")) is int and start <= row["requested_us"] < end]
    points = []
    for row in rows:
        box = observation_box(row)
        if not box:
            continue
        points.append({"time_us": max(0, int(row["requested_us"] - start)),
                       "center_x": round(box["x"] + box["w"] / 2, 6),
                       "center_y": round(box["y"] + box["h"] / 2, 6),
                       "bbox": box,
                       "evidence_id": evidence_id(str(aid), int(row["requested_us"]), "visual",
                                                    str(row.get("frame_sha256") or ""))})
    if not points:
        return {"source_segment_id": mapping.get("source_segment_id"), "asset_id": aid,
                "mode": "center_fallback", "confidence": 0.0, "keyframes": [],
                "occlusion_check": "unavailable", "reason": "no_verified_subject_bbox"}
    points.sort(key=lambda item: item["time_us"])
    confidence = min(1.0, 0.45 + 0.1 * len(points))
    return {"source_segment_id": mapping.get("source_segment_id"), "asset_id": aid,
            "mode": "subject_follow", "confidence": round(confidence, 3),
            "keyframes": points, "occlusion_check": "bbox_aware",
            "reason": "verified_subject_bbox_from_visual_evidence"}


def build_layout_tracks(timeline: dict[str, Any], observations_by_asset: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    observations_by_asset = observations_by_asset or {}
    return [_layout_for_span(mapping, observations_by_asset.get(mapping.get("asset_id"), []))
            for mapping in timeline.get("source_map", []) if isinstance(mapping, dict)]


def apply_supplements(task: dict[str, Any], timeline: dict[str, Any], assets: list[dict[str, Any]], *,
                      observations_by_asset: dict[str, Any] | None = None,
                      candidates: Iterable[dict[str, Any]] | None = None,
                      frames: Iterable[dict[str, Any]] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    observations_by_asset = observations_by_asset or {}
    all_observations = [row for value in observations_by_asset.values() for row in _observation_rows(value)]
    asset_frames = list(frames or [])
    evidence = []
    for frame in asset_frames:
        if not isinstance(frame, dict) or type(frame.get("requested_us")) is not int:
            continue
        aid = str(frame.get("asset_id") or "unknown")
        evidence.append({"evidence_id": evidence_id(aid, frame["requested_us"], "frame", str(frame.get("sha256") or "")),
                         "asset_id": aid, "time_us": frame["requested_us"], "type": "sampled_frame",
                         "sha256": frame.get("sha256"), "source": "framo_frame_evidence"})
    for row in all_observations:
        if type(row.get("requested_us")) is not int:
            continue
        aid = str(row.get("asset_id") or "unknown")
        evidence.append({"evidence_id": evidence_id(aid, row["requested_us"], "visual",
                                                       str(row.get("frame_sha256") or "")),
                         "asset_id": aid, "time_us": row["requested_us"], "type": "visual_observation",
                         "frame_sha256": row.get("frame_sha256"), "confidence": row.get("confidence"),
                         "subject": row.get("subject"), "bbox": observation_box(row),
                         "source": "framo_visual_observation"})
    layouts = build_layout_tracks(timeline, observations_by_asset)
    scored = list(candidates or [])
    # Montage and ordinary edits annotate candidates before this stage.  The
    # direct-ad planner, however, compiles its candidate rows after that
    # annotation point.  Fill only missing scores here so existing scores are
    # preserved while every workflow gets the same auditable metadata.
    if any(isinstance(row, dict) and "highlight_score" not in row for row in scored):
        scored = annotate_candidates(scored, observations=all_observations,
                                      frames=asset_frames)
    report = {
        "schema_version": SCHEMA_VERSION,
        "supplement_version": VERSION,
        "sources": [{"asset_id": asset.get("asset_id"), "sha256": asset.get("sha256"),
                     "frame_count": sum(1 for frame in asset_frames if frame.get("asset_id") == asset.get("asset_id")),
                     "observation_count": sum(1 for row in all_observations if row.get("asset_id") in (None, asset.get("asset_id")))}
                    for asset in assets],
        "evidence": evidence,
        "candidate_scores": scored,
        "layout_tracks": layouts,
        "components": {
            "video_highlighter": "signal_fusion_enabled",
            "video_evidence": "evidence_ids_and_provenance_enabled",
            "openclipper": "layout_tracks_enabled_when_subject_bbox_is_available",
        },
        "limitations": [
            "motion/audio/scene-change/object tracking are unavailable unless an external detector supplies them",
            "subject_follow falls back to center_fallback when no verified bbox exists",
            "semantic review remains separate from deterministic evidence",
        ],
    }
    merged = deepcopy(timeline)
    merged["supplement_version"] = VERSION
    merged["layout_tracks"] = layouts
    merged["supplemental_analysis"] = {"version": VERSION, "artifact": "supplemental-analysis.json"}
    return merged, report

