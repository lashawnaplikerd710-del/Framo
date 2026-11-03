"""Read-only MediaInfo profiles; metadata is evidence, not a quality verdict."""
import argparse
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path


SCHEMA = "jianying-material-technical-v1"


def _first(data, *keys):
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def _integer(value):
    result = _number(value)
    return int(result) if result is not None and result.is_integer() else None


def _seconds(data, key="duration"):
    value = _number(data.get(key))
    return value / 1000 if value is not None else None


def _rate(data):
    numerator = _integer(_first(data, "framerate_num", "frame_rate_num"))
    denominator = _integer(_first(data, "framerate_den", "frame_rate_den"))
    valid = numerator is not None and numerator > 0 and denominator is not None and denominator > 0
    fraction = Fraction(numerator, denominator) if valid else None
    return {
        "reported_fps": _number(data.get("frame_rate")),
        "reported_text": data.get("frame_rate"),
        "numerator": numerator,
        "denominator": denominator,
        "rational": f"{fraction.numerator}/{fraction.denominator}" if fraction is not None else None,
        "exact_fps_from_rational": numerator / denominator if valid else None,
        "rational_evidence": "explicit_metadata_numerator_denominator" if valid else None,
        "mode": data.get("frame_rate_mode"),
        "original_mode": _first(data, "framerate_mode_original", "frame_rate_mode_original"),
        "minimum_fps": _number(_first(data, "minimum_frame_rate", "frame_rate_minimum")),
        "maximum_fps": _number(_first(data, "maximum_frame_rate", "frame_rate_maximum")),
    }


def _common_track(data, index):
    return {
        "source_track_index": index,
        "track_id": data.get("track_id"),
        "stream_order": _first(data, "streamorder", "stream_order"),
        "format": data.get("format"),
        "codec_id": data.get("codec_id"),
        "codec_profile": data.get("format_profile"),
        "commercial_name": data.get("commercial_name"),
        "duration_seconds": _seconds(data),
        "source_duration_seconds": _seconds(data, "source_duration"),
        "bitrate_bps": _number(data.get("bit_rate")),
        "bitrate_mode": data.get("bit_rate_mode"),
        "bit_depth": _integer(data.get("bit_depth")),
        "language": data.get("language"),
        "default": data.get("default"),
        "delay_seconds": _seconds(data, "delay"),
        "source_delay_seconds": _seconds(data, "source_delay"),
    }


def _visual(data, index):
    result = _common_track(data, index)
    width, height = _integer(data.get("width")), _integer(data.get("height"))
    orientation = None
    if width and height:
        orientation = "landscape" if width > height else "portrait" if width < height else "square"
    result.update({
        "width": width, "height": height,
        "stored_width": _integer(data.get("stored_width")),
        "stored_height": _integer(data.get("stored_height")),
        "stored_orientation": orientation,
        "rotation_degrees": _number(data.get("rotation")),
        "pixel_aspect_ratio": _number(data.get("pixel_aspect_ratio")),
        "display_aspect_ratio": _number(data.get("display_aspect_ratio")),
        "frame_rate": _rate(data),
        "frame_count": _integer(data.get("frame_count")),
        "scan_type": data.get("scan_type"),
        "chroma_subsampling": data.get("chroma_subsampling"),
        "colour": {
            "pixel_model": _first(data, "color_space", "colour_space"),
            "primaries": _first(data, "color_primaries", "colour_primaries"),
            "transfer": data.get("transfer_characteristics"),
            "matrix": data.get("matrix_coefficients"),
            "range": _first(data, "color_range", "colour_range"),
            "primaries_source": _first(data, "colour_primaries_source", "color_primaries_source"),
            "transfer_source": data.get("transfer_characteristics_source"),
            "matrix_source": data.get("matrix_coefficients_source"),
            "range_source": _first(data, "colour_range_source", "color_range_source"),
            "hdr_format": data.get("hdr_format"),
            "hdr_profile": data.get("hdr_format_profile"),
            "hdr_level": data.get("hdr_format_level"),
            "hdr_compatibility": data.get("hdr_format_compatibility"),
            "mastering_display_primaries": data.get("mastering_display_color_primaries"),
            "mastering_display_luminance": data.get("mastering_display_luminance"),
            "maximum_content_light_level": data.get("maximum_content_light_level"),
            "maximum_frame_average_light_level": data.get("maximum_frameaverage_light_level"),
            "interpretation": "reported_tags_only; do_not_infer_Rec709_Log_or_HDR_from_resolution_or_bit_depth",
        },
    })
    return result


def profile_from_tracks(tracks):
    """Pure parser for MediaInfo Track objects or dictionaries, including unknowns."""
    raw = [t.to_data() if hasattr(t, "to_data") else dict(t) for t in tracks]
    result = {"schema": SCHEMA, "container": None, "video_tracks": [], "audio_tracks": [], "image_tracks": [],
              "other_track_types": [], "implications": []}
    for index, data in enumerate(raw):
        kind = data.get("track_type")
        if kind == "General" and result["container"] is None:
            result["container"] = {
                "source_track_index": index,
                "format": data.get("format"), "profile": data.get("format_profile"),
                "codec_id": data.get("codec_id"), "extension": data.get("file_extension"),
                "duration_seconds": _seconds(data), "size_bytes": _integer(data.get("file_size")),
                "overall_bitrate_bps": _number(data.get("overall_bit_rate")),
                "overall_bitrate_mode": data.get("overall_bit_rate_mode"),
                "mime_type": data.get("internet_media_type"),
            }
        elif kind in ("Video", "Image"):
            result["video_tracks" if kind == "Video" else "image_tracks"].append(_visual(data, index))
        elif kind == "Audio":
            audio = _common_track(data, index)
            audio.update({"sample_rate_hz": _number(data.get("sampling_rate")),
                          "channels": _integer(_first(data, "channel_s", "channels")),
                          "channel_layout": data.get("channel_layout"),
                          "channel_positions": data.get("channel_positions"),
                          "compression_mode": data.get("compression_mode"),
                          "samples_count": _integer(data.get("samples_count"))})
            result["audio_tracks"].append(audio)
        elif kind != "General":
            result["other_track_types"].append({"source_track_index": index, "type": kind})
    result["presence"] = {"video": bool(result["video_tracks"]), "audio": bool(result["audio_tracks"]),
                          "image": bool(result["image_tracks"])}
    result["assessment_limits"] = {
        "native_decode_support": "unverified", "native_playback": "unverified",
        "visual_quality": "unassessed", "stability_exposure_focus": "unassessed",
        "audio_quality_or_speech_content": "unassessed", "usable_ranges": "unassessed",
        "editorial_role": "undecided", "metadata_strings_are_untrusted_data": True,
    }
    def imply(code, message, evidence):
        result["implications"].append({"code": code, "message": message, "evidence": evidence})
    if not result["presence"]["audio"]:
        imply("no_audio_stream", "No audio stream was reported; plan audio separately if needed.", ["/audio_tracks"])
    if not result["presence"]["video"] and not result["presence"]["image"]:
        imply("no_visual_stream", "No video/image stream was reported; this file cannot supply a picture on its own.",
              ["/video_tracks", "/image_tracks"])
    if len(result["video_tracks"]) > 1 or len(result["audio_tracks"]) > 1:
        imply("multiple_streams", "Select and verify the intended streams in the native backend; do not silently merge their durations.",
              ["/video_tracks", "/audio_tracks"])
    for group in ("video_tracks", "image_tracks"):
        for index, track in enumerate(result[group]):
            ref = f"/{group}/{index}"
            rate = track["frame_rate"]
            if str(rate["mode"]).upper() == "VFR":
                imply("variable_frame_rate", "Use timestamps and native boundary checks; a single FPS does not establish frame timing.",
                      [ref + "/frame_rate"])
            rotation = track["rotation_degrees"]
            if rotation is not None and rotation % 360:
                imply("rotation_tag", "Check native orientation before fitting or cropping the image.", [ref + "/rotation_degrees"])
            colour = track["colour"]
            transfer = str(colour["transfer"] or "").upper()
            if colour["hdr_format"] or any(token in transfer for token in ("HLG", "PQ", "2084", "ARIB STD-B67")):
                imply("hdr_tags_require_validation", "HDR-related tags are present; verify the native colour pipeline before grading or mixing with SDR.",
                      [ref + "/colour/transfer", ref + "/colour/hdr_format"])
            if any(colour[k] is None for k in ("primaries", "transfer", "matrix")):
                imply("incomplete_colour_tags", "Missing colour tags stay unknown; do not assume Rec.709, a Log curve, or a LUT.",
                      [ref + "/colour"])
    if len(result["video_tracks"]) == 1 and len(result["audio_tracks"]) == 1:
        video_duration = result["video_tracks"][0]["duration_seconds"]
        audio_duration = result["audio_tracks"][0]["duration_seconds"]
        if video_duration is not None and audio_duration is not None and abs(video_duration - audio_duration) > 0.001:
            imply("stream_duration_difference", "Video and audio durations differ; check each source range independently. This alone does not prove desynchronization.",
                  ["/video_tracks/0/duration_seconds", "/audio_tracks/0/duration_seconds"])
    return result


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe(path):
    """Return JSON-serializable profile; never executes an editor or renderer."""
    from pymediainfo import MediaInfo
    source = Path(path).expanduser().resolve(strict=True)
    if not source.is_file():
        raise ValueError("Input must be a local media file")
    digest = _digest(source)
    before = source.stat()
    metadata = MediaInfo.parse(str(source))
    result = profile_from_tracks(metadata.tracks)
    native_version, native_library = None, None
    # Optional provenance only. pymediainfo exposes no public native-version API.
    try:
        library, handle, native_version, _ = MediaInfo._get_library()
        try:
            native_library = str(library._name)
        finally:
            library.MediaInfo_Delete(handle)
    except (AttributeError, OSError, RuntimeError, TypeError):
        pass
    after = source.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns or digest != _digest(source):
        raise RuntimeError("Source changed during metadata inspection; retry on a stable file")
    result["input"] = {"path": str(source), "sha256": digest, "bytes": after.st_size}
    result["provenance"] = {"tool": "pymediainfo", "version": importlib.metadata.version("pymediainfo"),
                            "native_library_version": native_version, "native_library": native_library,
                            "method": "metadata_parse_only", "observed_at": datetime.now(timezone.utc).isoformat(),
                            "playback_performed": False}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True, help="New JSON file; existing files are never replaced")
    args = parser.parse_args()
    output = Path(args.out).expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"Output exists: {output}")
    result = probe(args.input)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"output": str(output), "schema": result["schema"], "sha256": result["input"]["sha256"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
