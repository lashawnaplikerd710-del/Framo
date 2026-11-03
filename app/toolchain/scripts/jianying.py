"""Local Windows Jianying draft tools. Never controls the editor or exports natively."""
import argparse
import copy
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from common import PlanError, check_keys, load_plan, local_file, media_info, new_output, number, read_json, us, write_json


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def running_editor():
    if sys.platform != "win32":
        return False
    result = subprocess.run(["tasklist", "/FI", "IMAGENAME eq JianyingPro.exe", "/FO", "CSV", "/NH"],
                            capture_output=True, timeout=15)
    if result.returncode:
        raise PlanError("Cannot check the Jianying process; close the editor and retry")
    return b"jianyingpro.exe" in result.stdout.lower()


def doctor():
    local = Path(os.environ.get("LOCALAPPDATA", "~")).expanduser()
    user_data = local / "JianyingPro" / "User Data"
    candidates = []
    explicit = os.environ.get("JIANYING_DRAFT_ROOT")
    if explicit:
        candidates.append({"path": explicit, "source": "JIANYING_DRAFT_ROOT"})
    config = user_data / "Config" / "globalSetting"
    if config.is_file():
        for line in config.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            if line.startswith("currentCustomDraftPath="):
                candidates.append({"path": line.split("=", 1)[1].replace("\\\\", "\\"), "source": "Jianying settings"})
    candidates.append({"path": str(user_data / "Projects" / "com.lveditor.draft"), "source": "default"})
    for item in candidates:
        item["exists"] = Path(item["path"]).is_dir()
    deps = {}
    for package in ("pyJianYingDraft", "pymediainfo"):
        try:
            deps[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            deps[package] = None
    return {"platform": sys.platform, "python": sys.version.split()[0], "dependencies": deps,
            "draft_roots": candidates, "editor_running": running_editor(),
            "execution_policy": "CLI only; no GUI launch, no UI automation, no external renderer fallback",
            "native_headless_adapter": {"implemented": True, "tested_build": "11.5.0.14471",
                "current_runtime_check": "not_run_by_doctor", "entry": "native_service.py health"},
            "full_non_member_feature_coverage": False,
            "native_export_supported": False, "legacy_encrypted_draft_edit_supported": False,
            "native_draft_codec_entry": "native_draft_codec.py",
            "editor_acceptance": "Windows 11.5 requires a real open/save/reopen check; JSON validation is not editor acceptance"}


def content_file(folder):
    folder = Path(folder).resolve()
    if folder.is_file():
        return folder
    for name in ("draft_content.json", "draft_info.json"):
        if (folder / name).is_file():
            return folder / name
    raise PlanError("Draft has neither draft_content.json nor draft_info.json")


def inspect_draft(path):
    file = content_file(path)
    data = read_json(file)
    if not isinstance(data, dict) or not isinstance(data.get("tracks"), list) or not isinstance(data.get("materials"), dict):
        raise PlanError("Not a supported plaintext Jianying draft")
    texts = []
    for mat in data["materials"].get("texts", []):
        try:
            texts.append({"material_id": mat["id"], "text": json.loads(mat["content"])["text"]})
        except (KeyError, TypeError, ValueError):
            continue
    return {"file": str(file), "duration_seconds": data.get("duration", 0) / 1e6,
            "tracks": [{"type": t.get("type"), "segments": [{"segment_id": s.get("id"),
                        "material_id": s.get("material_id"), "range": s.get("target_timerange"),
                        "volume": s.get("volume")} for s in t.get("segments", [])]} for t in data["tracks"]],
            "texts": texts, "materials": [{"material_id": m.get("id"), "path": m.get("path")}
                for group in ("videos", "audios") for m in data["materials"].get(group, [])],
            "native_acceptance": "not_checked"}


def update_meta(folder, data, name, content_name="draft_content.json"):
    path = folder / "draft_meta_info.json"
    meta = read_json(path) if path.exists() else {}
    stamp = int(time.time() * 1e6)
    meta.update({"draft_id": data["id"], "draft_name": name, "draft_fold_path": str(folder),
                 "draft_root_path": str(folder.parent), "draft_json_file": str(folder / content_name),
                 "tm_draft_create": stamp, "tm_draft_modified": stamp, "tm_duration": data.get("duration", 0)})
    write_json(path, meta)


def build(plan_path, output):
    import pyJianYingDraft as d
    if importlib.metadata.version("pyJianYingDraft") != "0.3.0":
        raise PlanError("This adapter requires pyJianYingDraft==0.3.0; run setup.ps1")
    plan = load_plan(plan_path)
    out = new_output(output)
    canvas = plan["canvas"]
    script = d.DraftFolder(str(out)).create_draft("draft", canvas["width"], canvas["height"], canvas["fps"], maintrack_adsorb=False)
    folder = out / "draft"
    (folder / "media").mkdir()
    copied, manifest = {}, []
    for row in plan["clips"] + plan["audio"]:
        src = row["path"]
        if src not in copied:
            source_hash = digest(src)
            filename = source_hash[:16] + "_" + Path(src).name
            dst = folder / "media" / filename
            shutil.copy2(src, dst)
            if digest(dst) != source_hash:
                raise PlanError("Media changed during copying")
            copied[src] = str(dst)
            manifest.append({"path": str(dst.relative_to(out)), "sha256": source_hash})
        row["path"] = copied[src]
    track_refs = {}
    for group, kind in (("clips", d.TrackType.video), ("audio", d.TrackType.audio)):
        for row in plan[group]:
            track_key = group + ":" + row["track"]
            if track_key not in track_refs:
                track_refs[track_key] = script.append_track(d.TrackSpec(kind, track_key))
            target = d.Timerange(us(row["start"]), us(row["duration"] / row["speed"]))
            source = d.Timerange(us(row["in"]), us(row["duration"]))
            if group == "clips":
                mat = d.VideoMaterial(row["path"])
                scale = 1.0
                if row["fit"] == "cover":
                    x, y = canvas["width"] / mat.width, canvas["height"] / mat.height
                    scale = max(x, y) / min(x, y)
                seg = d.VideoSegment(mat, target, source_timerange=source, speed=row["speed"],
                                     volume=row["volume"], clip_settings=d.ClipSettings(scale_x=scale, scale_y=scale))
            else:
                seg = d.AudioSegment(row["path"], target, source_timerange=source, speed=row["speed"], volume=row["volume"])
            script.add_segment(seg, track_refs[track_key])
    # Separate overlapping subtitle cues into distinct editable tracks.
    subtitle_tracks = []
    for row in sorted(plan["subtitles"], key=lambda s: s["start"]):
        free = next((t for t in subtitle_tracks if t[0] <= row["start"]), None)
        if free is None:
            free = [0, script.append_track(d.TrackSpec(d.TrackType.text, f"subtitles:{len(subtitle_tracks) + 1}"))]
            subtitle_tracks.append(free)
        color = tuple(int(row["color"][i:i + 2], 16) / 255 for i in (1, 3, 5))
        seg = d.TextSegment(row["text"], d.Timerange(us(row["start"]), us(row["duration"])),
                            style=d.TextStyle(size=row["font_size"] / 6, color=color, align=1, auto_wrapping=True),
                            clip_settings=d.ClipSettings(transform_y=1 - 2 * row["y"]), border=d.TextBorder(width=15))
        script.add_segment(seg, free[1])
        free[0] = row["start"] + row["duration"]
    script.save()
    data = read_json(folder / "draft_content.json")
    data["id"] = str(uuid.uuid4()).upper()
    data["name"] = plan["name"]
    write_json(folder / "draft_content.json", data)
    update_meta(folder, data, plan["name"])
    write_json(out / "edit-plan.json", plan)
    write_json(out / "manifest.json", manifest)
    report = verify(out)
    write_json(out / "build-report.json", report)
    return report


def verify(build_dir):
    out = Path(build_dir).resolve()
    info = inspect_draft(out / "draft")
    data = read_json(info["file"])
    seen = set()
    materials = {m.get("id") for rows in data["materials"].values() if isinstance(rows, list)
                 for m in rows if isinstance(m, dict)}
    media_by_id = {m["id"]: m for kind in ("videos", "audios") for m in data["materials"].get(kind, [])}
    media_cache = {}
    for track in data["tracks"]:
        end = 0
        for seg in sorted(track.get("segments", []), key=lambda s: s["target_timerange"]["start"]):
            if seg["id"] in seen:
                raise PlanError("Duplicate segment ID")
            seen.add(seg["id"])
            r = seg["target_timerange"]
            number(r["start"], "target start")
            number(r["duration"], "target duration", positive=True)
            if r["start"] < end or r["duration"] <= 0:
                raise PlanError("Invalid or overlapping track timing")
            end = r["start"] + r["duration"]
            if end > data["duration"] or seg["material_id"] not in materials:
                raise PlanError("Missing material or segment exceeds draft duration")
            if track.get("type") in ("video", "audio"):
                source = seg.get("source_timerange")
                if not isinstance(source, dict):
                    raise PlanError("Audio/video segment has no source timerange")
                number(source.get("start"), "source start")
                number(source.get("duration"), "source duration", positive=True)
                speed = number(seg.get("speed", 1), "segment speed", 0.1, 10)
                number(seg.get("volume", 1), "segment volume", 0, 4)
                if abs(source["duration"] / speed - r["duration"]) > 1:
                    raise PlanError("Source, speed and target duration disagree")
                mat = media_by_id.get(seg["material_id"])
                if mat is None or not Path(mat.get("path", "")).is_file():
                    raise PlanError("Audio/video material file is missing")
                if mat["path"] not in media_cache:
                    media_cache[mat["path"]] = media_info(mat["path"])
                metadata = media_cache[mat["path"]]
                if not metadata["is_image"] and source["start"] + source["duration"] > us(metadata["duration"]) + 1000:
                    raise PlanError("Source range exceeds media duration")
    if not any(t.get("type") == "video" and t.get("segments") for t in data["tracks"]):
        raise PlanError("Draft has no visual track")
    for row in read_json(out / "manifest.json"):
        path = (out / row["path"]).resolve()
        if not path.is_relative_to(out) or not path.is_file() or digest(path) != row["sha256"]:
            raise PlanError("Missing/changed media or invalid manifest path")
    for row in info["materials"]:
        if row["path"] and not Path(row["path"]).is_file():
            raise PlanError(f"Missing draft material: {row['path']}")
    return {"status": "structural_checks_passed", "draft": str(out / "draft"), "segments": len(seen),
            "duration_seconds": info["duration_seconds"], "native_acceptance": "not_checked",
            "note": "Open, inspect, save and reopen in your installed Jianying before claiming editor compatibility."}


def remap_paths(value, source, dest):
    if isinstance(value, dict):
        return {k: remap_paths(v, source, dest) for k, v in value.items()}
    if isinstance(value, list):
        return [remap_paths(v, source, dest) for v in value]
    if isinstance(value, str):
        for old, new in ((str(source), str(dest)), (source.as_posix(), dest.as_posix())):
            if value == old or value.startswith(old + "/") or value.startswith(old + "\\"):
                return new + value[len(old):]
    return value


def safe_clone(source, dest):
    if dest.exists() or dest.is_relative_to(source) or source.is_relative_to(dest):
        raise PlanError("Choose a new output directory outside the source draft")
    for p in source.rglob("*"):
        if p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()):
            raise PlanError("Draft contains a link/junction; copy regular files explicitly first")
    shutil.copytree(source, dest)


def edit_copy(source, changes_path, output):
    file = content_file(source)
    data = read_json(file)
    inspect_draft(file)
    source = file.parent
    dest = Path(output).resolve()
    changes = read_json(changes_path)
    check_keys(changes, ["name", "text", "volume"], "changes")
    if "name" in changes and (not isinstance(changes["name"], str) or not changes["name"].strip()):
        raise PlanError("name must be a nonempty string")
    for field in ("text", "volume"):
        if not isinstance(changes.get(field, []), list):
            raise PlanError(f"changes.{field} must be an array")
    text_by_id = {m["id"]: m for m in data["materials"].get("texts", [])}
    segments = {s["id"]: s for t in data["tracks"] if t.get("type") in ("video", "audio") for s in t.get("segments", [])}
    for row in changes.get("text", []):
        check_keys(row, ["material_id", "text"], "text change")
        mat = text_by_id.get(row.get("material_id"))
        if mat is None or not isinstance(row.get("text"), str) or not row["text"]:
            raise PlanError("Text change needs an existing material_id and nonempty text")
        content = json.loads(mat["content"])
        styles = content.get("styles", [])
        if len(styles) != 1:
            raise PlanError("This editor supports only single-style text replacement; mixed-style text needs a dedicated adapter")
        content["text"] = row["text"]
        styles[0]["range"] = [0, len(row["text"])]
        mat["content"] = json.dumps(content, ensure_ascii=False)
    for row in changes.get("volume", []):
        check_keys(row, ["segment_id", "volume"], "volume change")
        seg = segments.get(row.get("segment_id"))
        if seg is None or "volume" not in seg:
            raise PlanError("Volume change needs an existing audio/video segment_id")
        seg["volume"] = number(row.get("volume"), "volume", 0, 4)
    safe_clone(source, dest)
    data = remap_paths(data, source, dest)
    data["id"] = str(uuid.uuid4()).upper()
    data["name"] = changes.get("name", data.get("name", "Draft") + " copy")
    write_json(dest / file.name, data)
    # Keep a matching backup only if the original already used it.
    backup = dest / (file.name + ".bak")
    if backup.exists():
        write_json(backup, data)
    update_meta(dest, data, data["name"], file.name)
    return {"status": "copy_edited", "draft": str(dest), "source_preserved": str(source), "native_acceptance": "not_checked"}


def place(build_dir, draft_root, name):
    verify(build_dir)
    if running_editor():
        raise PlanError("Jianying is running. Save your work and exit it before placing the draft. It will not be force-closed.")
    if not name or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((" ", ".")) or name in (".", ".."):
        raise PlanError("name must be a valid single Windows folder name")
    if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", name, re.I):
        raise PlanError("Reserved Windows folder name")
    root = Path(draft_root).resolve()
    if not root.is_dir():
        raise PlanError("The selected draft root must already exist")
    source = Path(build_dir).resolve() / "draft"
    dest = root / name
    safe_clone(source, dest)
    data = remap_paths(read_json(dest / "draft_content.json"), source, dest)
    data["id"] = str(uuid.uuid4()).upper()
    data["name"] = name
    write_json(dest / "draft_content.json", data)
    update_meta(dest, data, name)
    return {"status": "placed_for_editor_scan", "draft": str(dest), "native_acceptance": "not_checked",
            "note": "Reopen Jianying and find the new draft. No root index or existing project was modified."}


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    p = sub.add_parser("build"); p.add_argument("--plan", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("verify"); p.add_argument("--build", required=True)
    p = sub.add_parser("inspect"); p.add_argument("--draft", required=True)
    p = sub.add_parser("edit-copy"); p.add_argument("--draft", required=True); p.add_argument("--changes", required=True); p.add_argument("--out", required=True)
    p = sub.add_parser("place"); p.add_argument("--build", required=True); p.add_argument("--draft-root", required=True); p.add_argument("--name", required=True)
    args = parser.parse_args()
    try:
        if args.command == "doctor": result = doctor()
        elif args.command == "build": result = build(args.plan, args.out)
        elif args.command == "verify": result = verify(args.build)
        elif args.command == "inspect": result = inspect_draft(args.draft)
        elif args.command == "edit-copy": result = edit_copy(args.draft, args.changes, args.out)
        else: result = place(args.build, args.draft_root, args.name)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (PlanError, OSError, ValueError, KeyError, ImportError, subprocess.SubprocessError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
