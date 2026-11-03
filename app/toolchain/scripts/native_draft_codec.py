"""Encode/decode explicit draft-file copies using a verified local native codec.

No editor UI, FFmpeg, credentials, directory discovery or draft registration.
Only the tested DLL profile may execute. Other builds can be diagnosed without
loading their DLL. Success verifies the codec, not editor acceptance or effects.
"""

import argparse
import csv
import ctypes
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


TESTED_PROFILES = {
    "37cadef37daff82e2ecdcd65080b9cbd7f6f9436f5e56c1ffb90c52c408eb7fb": {
        "build": "11.5.0.14471",
        "verification": "Synthetic draft encode/decode byte roundtrip on Windows x64",
        "editor_acceptance": "not_checked",
    }
}
MAX_INPUT_BYTES = 128 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def child_environment():
    # Intentionally never copy the complete parent environment or read .env.
    return {key: os.environ[key] for key in
            ("SystemRoot", "WINDIR", "SystemDrive", "TEMP", "TMP")
            if key in os.environ}


def system_executable(relative):
    root = os.environ.get("SystemRoot")
    if not root:
        raise RuntimeError("SystemRoot is unavailable")
    result = Path(root) / "System32" / relative
    if not result.is_file():
        raise RuntimeError("Required Windows system executable is unavailable")
    return result


def hidden_run(command, *, timeout, env=None):
    return subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=timeout, env=env or child_environment(),
                          creationflags=subprocess.CREATE_NO_WINDOW)


def signature(path):
    environment = child_environment()
    environment["JY_CODEC_SIGNATURE_FILE"] = str(path)
    command = (
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        "$ErrorActionPreference='Stop';"
        "$s=Get-AuthenticodeSignature -LiteralPath $env:JY_CODEC_SIGNATURE_FILE;"
        "@{status=$s.Status.ToString();signer=$s.SignerCertificate.Subject;"
        "thumbprint=$s.SignerCertificate.Thumbprint}|ConvertTo-Json -Compress"
    )
    result = hidden_run([str(system_executable(
        "WindowsPowerShell/v1.0/powershell.exe")), "-NoProfile", "-NonInteractive",
        "-Command", command], timeout=15, env=environment)
    if result.returncode:
        raise RuntimeError("Authenticode verification failed")
    return json.loads(result.stdout.decode("utf-8-sig"))


def editor_pids():
    result = hidden_run([str(system_executable("tasklist.exe")), "/FI",
                         "IMAGENAME eq JianyingPro.exe", "/FO", "CSV", "/NH"],
                        timeout=10)
    if result.returncode:
        raise RuntimeError("Could not inspect editor process state")
    rows = csv.reader(result.stdout.decode("ascii", errors="replace").splitlines())
    return sorted(int(row[1]) for row in rows
                  if len(row) > 1 and row[0].lower() == "jianyingpro.exe")


def diagnose(install_dir):
    if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError("Windows with 64-bit Python is required")
    folder = Path(install_dir).resolve(strict=True)
    dll = (folder / "videoeditor.dll").resolve(strict=True)
    if not folder.is_dir() or not dll.is_file() or dll.parent != folder:
        raise RuntimeError("Supply the exact installed directory containing videoeditor.dll")
    before = file_digest(dll)
    sig = signature(dll)
    if file_digest(dll) != before:
        raise RuntimeError("DLL changed during signature verification")
    profile = TESTED_PROFILES.get(before)
    return {"install_dir": str(folder), "dll_path": str(dll),
            "dll_sha256": before, "signature": sig, "tested_profile": profile,
            "execution_allowed": bool(profile and sig.get("status") == "Valid"),
            "dll_loaded_during_diagnose": False}


def require_profile(info):
    if info["signature"].get("status") != "Valid":
        raise RuntimeError("Invalid DLL signature; execution refused")
    if not info["tested_profile"]:
        raise RuntimeError("Untested DLL SHA-256; diagnose only. No native code was loaded")


def validate_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Draft JSON has duplicate object keys")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("Draft JSON contains a non-finite number")

    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=pairs,
                       parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("Draft JSON must be a top-level object")
    return value


def read_input(path):
    if not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError("Input must be one regular file no larger than 128 MiB")
    with path.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if not raw or len(raw) > MAX_INPUT_BYTES:
        raise ValueError("Input is empty or exceeds the 128 MiB limit")
    return raw


def new_path(value):
    original = Path(value)
    if original.exists() or original.is_symlink():
        raise FileExistsError("Destination already exists; choose a new file")
    return original.resolve()


def write_new(path, raw):
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def worker(argv):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("operation", choices=("encode", "decode"))
    parser.add_argument("install_dir")
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("result")
    parser.add_argument("source_sha256")
    args = parser.parse_args(argv)
    info = diagnose(args.install_dir)
    require_profile(info)
    source = Path(args.input).resolve(strict=True)
    output, result_file = new_path(args.output), new_path(args.result)
    if output == result_file or source in (output, result_file):
        raise ValueError("Input, output and report must be distinct files")
    if any(path.is_relative_to(Path(info["install_dir"])) for path in (output, result_file)):
        raise ValueError("Output and report must be outside the editor installation")
    raw = read_input(source)
    if digest(raw) != args.source_sha256:
        raise RuntimeError("Source changed before isolated native operation")
    if args.operation == "encode":
        validate_json(raw)
    module_path = Path(__file__).parent / "vendor" / "jianying_crypto_bridge.py"
    spec = importlib.util.spec_from_file_location("_jianying_native_bridge", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Signature + exact DLL digest were verified before this first native load.
    codec = module.NativeDraftCodec(info["install_dir"])
    converted = codec.encode(raw) if args.operation == "encode" else codec.decode(raw)
    if args.operation == "decode":
        validate_json(converted)
    if file_digest(source) != args.source_sha256:
        raise RuntimeError("Source changed during isolated native operation")
    if file_digest(Path(info["dll_path"])) != info["dll_sha256"]:
        raise RuntimeError("DLL changed during isolated native operation")
    write_new(output, converted)
    write_new(result_file, json.dumps({
        "source_sha256": digest(raw), "output_sha256": digest(converted),
        "json_object_validated": True,
        "native_encode_decode_byte_roundtrip": args.operation == "encode",
        "dll_sha256": info["dll_sha256"],
    }).encode("utf-8"))
    return 0


def convert(args, info, report):
    require_profile(info)
    source = Path(args.input).resolve(strict=True)
    output = new_path(args.output)
    report_path = Path(args.report).resolve()
    if source in (output, report_path) or output == report_path:
        raise ValueError("Input, output and report must be distinct files")
    install_dir = Path(info["install_dir"])
    if output.is_relative_to(install_dir) or report_path.is_relative_to(install_dir):
        raise ValueError("Output and report must be outside the editor installation")
    raw = read_input(source)
    if args.operation == "encode":
        validate_json(raw)
    source_hash = digest(raw)
    report.update({"source_path": str(source), "source_sha256": source_hash,
                   "output_path": str(output), "source_size": len(raw)})
    before = editor_pids()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".native-codec-", dir=output.parent) as stage:
        staged_output = Path(stage) / "converted.bin"
        staged_report = Path(stage) / "verified.json"
        command = [sys.executable, "-I", "-S", str(Path(__file__).resolve()),
                   "_worker", args.operation, info["install_dir"], str(source),
                   str(staged_output), str(staged_report), source_hash]
        try:
            result = hidden_run(command, timeout=args.timeout)
        finally:
            after = editor_pids()
            report.update({"editor_running_before": bool(before),
                           "new_editor_pids": sorted(set(after) - set(before))})
        if report["new_editor_pids"]:
            raise RuntimeError("Unexpected new editor process; result was not published")
        report["child_exit_code"] = result.returncode
        if result.returncode:
            raise RuntimeError("Isolated native codec failed; result was not published")
        verified = json.loads(staged_report.read_text(encoding="utf-8"))
        converted = staged_output.read_bytes()
        if (verified["source_sha256"] != source_hash or
                verified["output_sha256"] != digest(converted) or
                verified["dll_sha256"] != info["dll_sha256"]):
            raise RuntimeError("Isolated codec verification report mismatch")
        if args.operation == "encode" and not verified["native_encode_decode_byte_roundtrip"]:
            raise RuntimeError("Required native byte roundtrip was not verified")
        if args.operation == "decode":
            validate_json(converted)
        if file_digest(source) != source_hash:
            raise RuntimeError("Source changed; result was not published")
        write_new(output, converted)
        report.update(verified)
        report.update({"output_size": len(converted), "source_preserved": True,
                       "output_created": True, "state": "codec_verified"})


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "_worker":
        try:
            return worker(argv[1:])
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            print(json.dumps({"state": "worker_refused", "error": str(exc)}), file=sys.stderr)
            return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("diagnose", "encode", "decode"))
    parser.add_argument("--install-dir", required=True, help="Explicit installed build directory")
    parser.add_argument("--input", help="One source file; never changed")
    parser.add_argument("--output", help="New destination file; no overwrite")
    parser.add_argument("--report", help="New JSON report; required for encode/decode")
    parser.add_argument("--timeout", type=int, default=45, help="Worker timeout: 1 to 45 seconds")
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 45:
        parser.error("--timeout must be between 1 and 45 seconds")
    if args.operation != "diagnose" and not all((args.input, args.output, args.report)):
        parser.error("encode/decode require --input, --output and --report")
    if args.operation == "diagnose" and (args.input or args.output):
        parser.error("diagnose does not accept input/output files")
    report = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "operation": args.operation, "state": "refused", "output_created": False,
              "validation_scope": "Native draft-file codec only; not editor acceptance",
              "native_editor_acceptance": "not_checked", "native_rendering": "not_checked",
              "all_non_member_features_verified": False, "gui_start_requested": False,
              "ffmpeg_used": False, "draft_registration_performed": False}
    report_stream = None
    try:
        if args.report:
            report_path = new_path(args.report)
            source = Path(args.input).resolve() if args.input else None
            output = Path(args.output).resolve() if args.output else None
            if report_path in (source, output):
                raise ValueError("Report must be distinct from input and output")
            if report_path.is_relative_to(Path(args.install_dir).resolve()):
                raise ValueError("Report must be outside the editor installation")
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_stream = report_path.open("x", encoding="utf-8")
        info = diagnose(args.install_dir)
        report.update(info)
        if args.operation == "diagnose":
            report["state"] = "diagnosed_supported" if info["execution_allowed"] else "diagnosed_unapproved"
        else:
            convert(args, info, report)
        code = 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
        code = 2
    finally:
        if report_stream is not None:
            with report_stream:
                json.dump(report, report_stream, ensure_ascii=False, indent=2)
                report_stream.write("\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
