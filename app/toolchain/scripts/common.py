"""Validated local edit plans for the offline Jianying draft backend."""
import copy
import json
import math
from pathlib import Path
import re


class PlanError(ValueError):
    pass


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PlanError(f"Not readable UTF-8 JSON: {path}. Encrypted Jianying drafts are not supported.") from exc


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def number(value, label, low=0, high=None, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise PlanError(f"{label} must be a finite number")
    if value < low or (positive and value <= low) or (high is not None and value > high):
        raise PlanError(f"{label} is outside the supported range")
    return float(value)


def local_file(value, base):
    if not isinstance(value, str) or not value.strip() or "://" in value or value.startswith(("\\\\", "//")):
        raise PlanError("A local file path is required; URLs and network shares are not accepted")
    path = Path(value).expanduser()
    path = (base / path).resolve() if not path.is_absolute() else path.resolve()
    if not path.is_file():
        raise PlanError(f"Missing local file: {path}")
    return str(path)


def check_keys(data, allowed, label):
    if not isinstance(data, dict):
        raise PlanError(f"{label} must be an object")
    unknown = set(data) - set(allowed)
    if unknown:
        raise PlanError(f"Unsupported {label} fields: {', '.join(sorted(unknown))}")


def media_info(path):
    from pymediainfo import MediaInfo
    tracks = MediaInfo.parse(str(path)).tracks
    v = next((t for t in tracks if t.track_type in ("Video", "Image")), None)
    a = next((t for t in tracks if t.track_type == "Audio"), None)
    duration = max([float(t.duration or 0) / 1000 for t in tracks] or [0])
    return {"duration": duration, "width": int(v.width or 0) if v else 0,
            "video_duration": float(v.duration) / 1000 if v and v.duration else None,
            "audio_duration": float(a.duration) / 1000 if a and a.duration else None,
            "height": int(v.height or 0) if v else 0, "has_audio": a is not None,
            "has_video": v is not None, "rotation": float(v.rotation or 0) if v else 0,
            "is_image": v is not None and v.track_type == "Image"}


def load_plan(path):
    path = Path(path).resolve()
    plan = read_json(path)
    check_keys(plan, ["version", "name", "canvas", "clips", "audio", "subtitles"], "plan")
    if plan.get("version") != 1:
        raise PlanError("plan.version must be 1")
    name = plan.get("name", "Windows Jianying Draft")
    if not isinstance(name, str) or not name.strip():
        raise PlanError("name must be a nonempty string")
    canvas = plan.get("canvas", {"width": 1080, "height": 1920, "fps": 30})
    check_keys(canvas, ["width", "height", "fps"], "canvas")
    for key, default, hi in [("width", 1080, 7680), ("height", 1920, 7680), ("fps", 30, 120)]:
        val = canvas.get(key, default)
        number(val, f"canvas.{key}", 1, hi)
        if int(val) != val or (key != "fps" and int(val) % 2):
            raise PlanError("Canvas dimensions must be even integers; fps must be an integer")
        canvas[key] = int(val)
    result = {"version": 1, "name": name, "canvas": canvas, "clips": [], "audio": [], "subtitles": []}
    info_cache = {}
    for group in ("clips", "audio"):
        rows = plan.get(group, [])
        if not isinstance(rows, list):
            raise PlanError(f"{group} must be an array")
        next_start = 0.0
        for index, row in enumerate(rows):
            check_keys(row, ["path", "kind", "track", "start", "in", "duration", "speed", "volume", "fit"]
                       if group == "clips" else ["path", "track", "start", "in", "duration", "speed", "volume"], group)
            item = copy.deepcopy(row)
            item["path"] = local_file(row.get("path"), path.parent)
            if item["path"] not in info_cache:
                info_cache[item["path"]] = media_info(item["path"])
            meta = info_cache[item["path"]]
            if group == "clips":
                item["kind"] = row.get("kind", "image" if meta["is_image"] else "video")
                if item["kind"] not in ("image", "video") or not meta["has_video"]:
                    raise PlanError(f"Unsupported visual media: {item['path']}")
                if (item["kind"] == "image") != meta["is_image"]:
                    raise PlanError("kind does not match the detected image/video format")
                if meta["rotation"] % 360:
                    raise PlanError("Normalize rotation metadata before building this plan")
                item["fit"] = row.get("fit", "contain")
                if item["fit"] not in ("contain", "cover"):
                    raise PlanError("fit must be contain or cover")
            elif not meta["has_audio"]:
                raise PlanError(f"No audio stream: {item['path']}")
            item["track"] = row.get("track", "main" if group == "clips" else f"audio_{index + 1}")
            if not isinstance(item["track"], str) or not item["track"].strip():
                raise PlanError("track must be a nonempty string")
            item["start"] = number(row.get("start", next_start if group == "clips" else 0), "start")
            item["in"] = number(row.get("in", 0), "in")
            item["duration"] = number(row.get("duration"), "duration", positive=True)
            item["speed"] = number(row.get("speed", 1), "speed", 0.1, 10)
            item["volume"] = number(row.get("volume", 1), "volume", 0, 4)
            if item.get("kind") == "image":
                if item["in"] != 0 or item["speed"] != 1:
                    raise PlanError("Images require in=0 and speed=1")
            elif item["in"] + item["duration"] > meta["duration"] + 0.001:
                raise PlanError(f"Source range exceeds media duration: {item['path']}")
            if round(item["duration"] / item["speed"] * 1e6) < 1:
                raise PlanError("Segment is shorter than one microsecond")
            next_start = item["start"] + item["duration"] / item["speed"]
            result[group].append(item)
        for track in {x["track"] for x in result[group]}:
            end = 0.0
            for row in sorted((x for x in result[group] if x["track"] == track), key=lambda x: x["start"]):
                if row["start"] < end - 0.000001:
                    raise PlanError(f"Overlapping segments on {group}/{track}; use separate tracks")
                end = row["start"] + row["duration"] / row["speed"]
    if not result["clips"]:
        raise PlanError("At least one visual clip is required")
    visual_end = max(x["start"] + x["duration"] / x["speed"] for x in result["clips"])
    for item in result["audio"]:
        if item["start"] + item["duration"] / item["speed"] > visual_end + 0.000001:
            raise PlanError("Audio extends beyond the visual timeline; trim it explicitly")
    subs = plan.get("subtitles", [])
    if not isinstance(subs, list):
        raise PlanError("subtitles must be an array")
    for sub in subs:
        check_keys(sub, ["text", "start", "duration", "font_size", "color", "y"], "subtitle")
        if not isinstance(sub.get("text"), str) or not sub["text"].strip():
            raise PlanError("Subtitle text must not be empty")
        item = {"text": sub["text"], "start": number(sub.get("start"), "subtitle.start"),
                "duration": number(sub.get("duration"), "subtitle.duration", positive=True),
                "font_size": number(sub.get("font_size", 48), "font_size", 8, 400),
                "y": number(sub.get("y", 0.8), "subtitle.y", 0, 1), "color": sub.get("color", "#FFFFFF")}
        if not isinstance(item["color"], str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", item["color"]):
            raise PlanError("Subtitle color must be #RRGGBB")
        if item["start"] + item["duration"] > visual_end + 0.000001:
            raise PlanError("Subtitle extends beyond the visual timeline")
        result["subtitles"].append(item)
    return result


def new_output(path):
    target = Path(path).resolve()
    if target.exists():
        raise PlanError(f"Output already exists; choose a new directory: {target}")
    target.mkdir(parents=True, exist_ok=False)
    return target


def us(seconds):
    return round(seconds * 1_000_000)
