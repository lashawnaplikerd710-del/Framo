"""Compile source-time keep ranges and optional SRT into a validated edit plan.

All times are seconds. Range duration is measured before speed changes. Ranges
are played in their supplied order, so reordering and deliberate repeats work.
Subtitles keep their original text: cuts cannot establish word-level alignment.
The JSON report on stdout lists cues that need a semantic review.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys
import tempfile

from common import PlanError, check_keys, load_plan, local_file, media_info, number, read_json


_TIMECODE = r"(\d{2,}):([0-5]\d):([0-5]\d)[,.](\d{3})"
_TIMING_LINE = re.compile(r"^\s*" + _TIMECODE + r"\s*-->\s*" + _TIMECODE + r"\s*$")


def parse_srt(text):
    """Read ordinary SRT cues, rejecting malformed blocks instead of dropping them."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    cues = []
    for block_number, block in enumerate(re.split(r"\n[ \t]*\n+", text), 1):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        match = _TIMING_LINE.fullmatch(lines[0]) if lines else None
        if match is None or len(lines) < 2 or not "\n".join(lines[1:]).strip():
            raise PlanError(f"Malformed SRT block {block_number}; expected a timing line and text")
        fields = [int(x) for x in match.groups()]
        start = fields[0] * 3600 + fields[1] * 60 + fields[2] + fields[3] / 1000
        end = fields[4] * 3600 + fields[5] * 60 + fields[6] + fields[7] / 1000
        if end <= start:
            raise PlanError(f"SRT block {block_number} must end after it starts")
        cues.append({"cue": block_number, "start": start, "end": end,
                     "text": "\n".join(lines[1:]).strip()})
    return cues


def normalize_ranges(rows):
    if not isinstance(rows, list) or not rows:
        raise PlanError("The ranges file must contain a nonempty JSON array")
    ranges = []
    cursor = 0.0
    for index, row in enumerate(rows, 1):
        check_keys(row, ["in", "duration", "speed"], f"range {index}")
        source_in = number(row.get("in"), f"range {index}.in")
        duration = number(row.get("duration"), f"range {index}.duration", positive=True)
        speed = number(row.get("speed", 1), f"range {index}.speed", 0.1, 10)
        if round(duration / speed * 1_000_000) < 1:
            raise PlanError(f"Range {index} is shorter than one output microsecond")
        ranges.append({"in": source_in, "duration": duration, "speed": speed, "start": cursor})
        cursor += duration / speed
    return ranges


def remap_subtitles(cues, ranges):
    """Intersect each cue with each retained range and map it into output time."""
    fragments = []
    fragments_per_cue = Counter()
    shortened = set()
    submicrosecond = set()
    for range_index, selected in enumerate(ranges, 1):
        source_start = selected["in"]
        source_end = source_start + selected["duration"]
        for cue in cues:
            left = max(source_start, cue["start"])
            right = min(source_end, cue["end"])
            if right <= left:
                continue
            duration = (right - left) / selected["speed"]
            if round(duration * 1_000_000) < 1:
                submicrosecond.add(cue["cue"])
                continue
            start = selected["start"] + (left - source_start) / selected["speed"]
            fragments.append({"cue": cue["cue"], "range": range_index,
                              "subtitle": {"text": cue["text"], "start": start, "duration": duration}})
            fragments_per_cue[cue["cue"]] += 1
            if left > cue["start"] + 1e-9 or right < cue["end"] - 1e-9:
                shortened.add(cue["cue"])
    fragments.sort(key=lambda x: (x["subtitle"]["start"], x["range"], x["cue"]))
    multiple = {cue for cue, count in fragments_per_cue.items() if count > 1}
    review_cues = sorted(shortened | multiple | submicrosecond)
    report = {
        "source_cues": len(cues),
        "output_fragments": len(fragments),
        "removed_cues": [cue["cue"] for cue in cues if not fragments_per_cue[cue["cue"]]],
        "shortened_cues": sorted(shortened),
        "split_or_repeated_cues": sorted(multiple),
        "submicrosecond_fragments_dropped": sorted(submicrosecond),
        "semantic_review_required": bool(review_cues),
        "review_cues": review_cues,
        "warnings": (["Cut or repeated cues retain their complete original text. Review wording and timing "
                      "against the retained speech; this is time mapping, not word-level semantic editing."]
                     if review_cues else []),
    }
    return [fragment["subtitle"] for fragment in fragments], report


def compile_keep(source, ranges_path, output_path, srt_path=None, name=None,
                 width=None, height=None, fps=30):
    """Create one new plan; validate source bounds before committing the output."""
    output = Path(output_path).expanduser().resolve()
    if output.exists():
        raise PlanError(f"Output already exists; choose a new filename: {output}")
    source = local_file(str(source), Path.cwd())
    ranges_path = local_file(str(ranges_path), Path.cwd())
    ranges = normalize_ranges(read_json(ranges_path))
    metadata = media_info(source)
    if not metadata["has_video"] or metadata["is_image"]:
        raise PlanError("--source must be a local video, not an image or audio file")
    if (width is None) != (height is None):
        raise PlanError("Supply both --width and --height, or omit both to use the source dimensions")
    canvas = {"width": metadata["width"] if width is None else width,
              "height": metadata["height"] if height is None else height, "fps": fps}
    cues = []
    if srt_path is not None:
        srt_path = local_file(str(srt_path), Path.cwd())
        try:
            cues = parse_srt(Path(srt_path).read_text(encoding="utf-8-sig"))
        except UnicodeError as exc:
            raise PlanError("The SRT must be UTF-8; convert its encoding before compiling") from exc
    subtitles, subtitle_report = remap_subtitles(cues, ranges)
    plan = {"version": 1, "name": name or output.stem, "canvas": canvas,
            "clips": [dict(row, path=source, kind="video", track="main") for row in ranges],
            "audio": [], "subtitles": subtitles}

    # Validation uses an isolated temporary file because load_plan accepts a path.
    # Every media reference is absolute, so this temporary directory changes no paths.
    with tempfile.TemporaryDirectory(prefix="jianying-keep-") as temporary:
        validation_path = Path(temporary) / "plan.json"
        validation_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        validated = load_plan(validation_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation closes the check/write race and never truncates an existing file.
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(validated, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {"ok": True, "plan": str(output), "source": source,
            "clips": len(validated["clips"]),
            "duration": sum(row["duration"] / row["speed"] for row in ranges),
            "subtitles": subtitle_report}


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Local source video")
    parser.add_argument("--ranges", required=True, help="JSON array of {in, duration, speed?}; seconds")
    parser.add_argument("--srt", help="Optional UTF-8 SRT timed to the unedited source")
    parser.add_argument("--out", required=True, help="New plan JSON filename; never overwritten")
    parser.add_argument("--name", help="Draft name; defaults to the output filename stem")
    parser.add_argument("--width", type=int, help="Canvas width; supply with --height")
    parser.add_argument("--height", type=int, help="Canvas height; supply with --width")
    parser.add_argument("--fps", type=int, default=30, help="Output frame rate (default: 30)")
    args = parser.parse_args(argv)
    try:
        result = compile_keep(args.source, args.ranges, args.out, args.srt, args.name,
                              args.width, args.height, args.fps)
    except (PlanError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
