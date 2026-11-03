"""One read-only --help probe of a signed installed Lyra CLI; never authenticates or edits."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def classify(returncode, stdout):
    response = None
    for line in stdout.splitlines():
        try:
            candidate = json.loads(line)
            if isinstance(candidate, dict):
                response = candidate
                break
        except ValueError:
            pass
    if response and (response.get("err_code") == -100015 or "unauthorized host process" in str(response.get("err_msg", "")).lower()):
        return {"state": "blocked_host_authorization", "native_interface_connected": False,
                "error": {k: response.get(k) for k in ("err_category", "err_code", "err_msg", "recovery")},
                "next_requirement": "A vendor-supported standalone CLI/SDK host entry point; do not spoof the host or runtime token."}
    if returncode == 0 and ("usage" in stdout.lower() or "options" in stdout.lower()):
        return {"state": "help_available_only", "native_interface_connected": False,
                "next_requirement": "Verify documented commands on isolated drafts. Help is not a feature or compatibility test."}
    return {"state": "probe_failed_or_unrecognized", "native_interface_connected": False,
            "error": {k: response.get(k) for k in ("err_category", "err_code", "err_msg", "recovery")} if response else None}


def editor_pids():
    r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq JianyingPro.exe", "/FO", "CSV", "/NH"],
                       capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode:
        raise RuntimeError("Cannot inspect existing editor processes")
    rows = csv.reader(r.stdout.decode("ascii", errors="replace").splitlines())
    return sorted(int(row[1]) for row in rows if len(row) > 1 and row[0].lower() == "jianyingpro.exe")


def signature(path):
    env = dict(os.environ, JY_CLI_PROBE_FILE=str(path))
    command = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;$s=Get-AuthenticodeSignature -LiteralPath $env:JY_CLI_PROBE_FILE; @{status=$s.Status.ToString();signer=$s.SignerCertificate.Subject}|ConvertTo-Json -Compress"
    shell = shutil.which("pwsh.exe") or shutil.which("powershell.exe")
    if not shell:
        raise RuntimeError("PowerShell is required to verify the CLI signature")
    if Path(shell).name.lower() == "powershell.exe":
        env.pop("PSModulePath", None)  # Let Windows PowerShell use its own built-in modules.
    r = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", command], env=env,
                       capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    if r.returncode:
        raise RuntimeError("Cannot verify the installed CLI signature")
    return json.loads(r.stdout.decode("utf-8-sig", errors="replace").strip())


def probe(install_dir):
    if sys.platform != "win32":
        raise RuntimeError("This probe requires Windows")
    exe = Path(install_dir).resolve() / "lyra-cli.exe"
    if not exe.is_file():
        raise FileNotFoundError(exe)
    sig = signature(exe)
    if sig.get("status") != "Valid":
        raise RuntimeError("CLI signature is not valid; the probe will not execute it")
    before = editor_pids()
    # Only --help is allowed. No auth commands, injected credentials, actual draft or cloud tasks.
    r = subprocess.run([str(exe), "--help"], cwd=exe.parent, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=25, creationflags=subprocess.CREATE_NO_WINDOW)
    after = editor_pids()
    stdout = r.stdout.decode("utf-8", errors="replace")
    result = classify(r.returncode, stdout)
    result.update({"timestamp_utc": datetime.now(timezone.utc).isoformat(),
                   "executable": str(exe), "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
                   "signature": sig, "command": [str(exe), "--help"], "exit_code": r.returncode,
                   "editor_running_before": bool(before), "new_editor_pids": sorted(set(after) - set(before)),
                   "gui_independent_operation_verified": False, "all_non_member_features_verified": False})
    if result["new_editor_pids"]:
        result["state"] = "unexpected_editor_process"
    return result


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--out", required=True, help="New JSON report path; never overwrite")
    args = parser.parse_args()
    output = Path(args.out).resolve()
    if output.exists():
        print("Choose a new report path; existing files are preserved.", file=sys.stderr)
        return 2
    try:
        result = probe(args.install_dir)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["state"] == "help_available_only" else 2
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"state": "probe_error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
