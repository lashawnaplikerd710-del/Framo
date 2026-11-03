"""Own an isolated, pinned Jianying native service; never start the editor UI.

Library: with Service(install, new_scratch) as service:
    response = service.request('system', 'health')
CLI: native_service.py health --install-dir VERSION_DIR --out NEW_DIRECTORY
"""
from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from native_draft_codec import editor_pids, file_digest, signature


PROFILE = {
    "build": "11.5.0.14471",
    "videoeditor.dll": "37cadef37daff82e2ecdcd65080b9cbd7f6f9436f5e56c1ffb90c52c408eb7fb",
    "videoeditor_addon.node": "7f5d59204957391ab7a4d9ec69a45ee2a70edc4707ff2134f3185bb3e245ff78",
    "Resources/canvas_agent/runtime/videoeditor-service.mjs": "83a1b2515b3ebac99e6a0d83b8040c361f593cde29a437aa5b1c02e673b2e585",
}
MAX_JSON_BYTES = 128 * 1024 * 1024


class NativeServiceError(RuntimeError):
    """A native request was rejected, failed, or could not be validated."""


def validate_player_state(state, duration_us, canvas):
    """Check loaded timing and preview aspect, after draft canvas roundtrip.

    frameWidth/frameHeight describe the preview, not the saved project canvas.
    This build returns 360x640 for a 1080x1920 draft by default. Keep the exact
    canvas check in the persisted snapshot; allow proportional preview reduction.
    """
    width, height = canvas.get('width'), canvas.get('height')
    fw, fh = state.get('frameWidth'), state.get('frameHeight')
    if (state.get('accepted') is not True or state.get('status') != 'ready'
            or state.get('durationUs') != duration_us
            or any(type(n) is not int or n <= 0 for n in (width, height, fw, fh))):
        raise NativeServiceError('Reloaded player is not ready with valid duration and dimensions')
    if fw > width or fh > height or abs(fw * height - fh * width) > max(width, height):
        raise NativeServiceError('Native preview size/aspect does not match the saved project canvas')
    return {'project_width': width, 'project_height': height,
            'preview_width': fw, 'preview_height': fh,
            'preview_scaled': (fw, fh) != (width, height)}


def clean_environment(install: Path, scratch: Path) -> dict[str, str]:
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT",
               "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        raise NativeServiceError("Windows SystemRoot is unavailable")
    temp = scratch / "temp"
    temp.mkdir(exist_ok=False)
    env.update(PATH=str(install) + os.pathsep + str(Path(system_root) / "System32"),
               TEMP=str(temp), TMP=str(temp),
               VIDEOEDITOR_ADDON_PATH=str(install / "videoeditor_addon.node"),
               VIDEOEDITOR_MODELS_DIR=str(install / "Resources/models"))
    return env


def verify_install(install: Path) -> dict:
    components = {}
    for relative, expected in PROFILE.items():
        if relative == "build":
            continue
        path = (install / relative).resolve(strict=True)
        if not path.is_file() or not path.is_relative_to(install):
            raise NativeServiceError(f"Invalid native component: {relative}")
        actual = file_digest(path)
        if actual != expected:
            raise NativeServiceError(f"Untested component SHA-256: {relative}; execution refused")
        components[relative] = {"sha256": actual}
    for relative in ("bun.exe", "videoeditor.dll"):
        path = (install / relative).resolve(strict=True)
        if not path.is_file() or path.parent != install:
            raise NativeServiceError(f"Missing installed component: {relative}")
        before = file_digest(path)
        sig = signature(path)
        if sig.get("status") != "Valid" or file_digest(path) != before:
            raise NativeServiceError(f"Invalid or changing Authenticode signature: {relative}")
        components.setdefault(relative, {})["signature"] = sig
        components[relative]["sha256"] = before
    if not (install / "Resources/models").is_dir():
        raise NativeServiceError("Installed native models directory is missing")
    return {"build": PROFILE["build"], "components": components}


class Service:
    """Context manager for one new scratch root and one process owned by it.

    request() returns the complete response envelope and raises on either
    ok != true or result.accepted == false. No existing service is discovered
    or reused. The caller must only send operations for authorized draft copies.
    """

    def __init__(self, install, scratch, *, startup_timeout=30, request_timeout=30):
        if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise NativeServiceError("Windows with 64-bit Python is required")
        raw_scratch = Path(scratch)
        if raw_scratch.exists() or raw_scratch.is_symlink():
            raise FileExistsError("Scratch/output directory already exists; choose a new directory")
        self.install = Path(install).resolve(strict=True)
        self.scratch = raw_scratch.resolve()
        if self.scratch == self.install or self.scratch.is_relative_to(self.install):
            raise NativeServiceError("Scratch must be outside the installed application")
        if not self.install.is_dir():
            raise NativeServiceError("Supply the exact installed version directory")
        self.startup_timeout = float(startup_timeout)
        self.request_timeout = float(request_timeout)
        if not 1 <= self.startup_timeout <= 120 or not 0.1 <= self.request_timeout <= 120:
            raise ValueError("Timeouts must be bounded between 0.1/1 and 120 seconds")
        self.endpoint = r"\\.\pipe\codex-jianying-native-" + uuid.uuid4().hex
        self.process = None
        self.health = None
        self.env = None
        self._stdout = self._stderr = None
        self._started = self._closed = self._created_scratch = False
        self._before = []
        self.report = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "install_dir": str(self.install), "scratch": str(self.scratch),
            "calls": [], "native_editor_acceptance": "not_checked",
            "all_non_member_features_verified": False,
        }

    @property
    def pid(self):
        return self.process.pid if self.process else None

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_value is not None:
            self.report["exception"] = str(exc_value)
        try:
            self.close()
        except Exception:
            if exc_value is None:
                raise
        return False

    def start(self):
        if self._started or self._closed:
            raise NativeServiceError("Service contexts cannot be reused")
        # Validate before executing any application code.
        profile = verify_install(self.install)
        if self.scratch.exists() or self.scratch.is_symlink():
            raise FileExistsError("Scratch/output directory appeared during validation")
        self.scratch.mkdir(parents=True, exist_ok=False)
        self._created_scratch = True
        self.report["profile"] = profile
        try:
            self._before = editor_pids()
            self.report["editor_running_before"] = bool(self._before)
            self.env = clean_environment(self.install, self.scratch)
            self._stdout = (self.scratch / "stdout.log").open("xb")
            self._stderr = (self.scratch / "stderr.log").open("xb")
            # Recheck pinned components after signature verification and before launch.
            for relative, item in profile["components"].items():
                if file_digest(self.install / relative) != item["sha256"]:
                    raise NativeServiceError(f"Native component changed before launch: {relative}")
            command = [str(self.install / "bun.exe"), str(self.install /
                       "Resources/canvas_agent/runtime/videoeditor-service.mjs"),
                       "--endpoint", self.endpoint]
            self.process = subprocess.Popen(
                command, env=self.env, cwd=self.scratch, stdout=self._stdout,
                stderr=self._stderr, stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            self._started = True
            self.report["pid"] = self.pid
            deadline = time.monotonic() + self.startup_timeout
            wait_pipe = ctypes.windll.kernel32.WaitNamedPipeW
            wait_pipe.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
            wait_pipe.restype = ctypes.c_int
            while self.process.poll() is None and time.monotonic() < deadline:
                if wait_pipe(self.endpoint, 50):
                    break
                time.sleep(0.05)
            else:
                raise NativeServiceError("Native service did not create its isolated pipe")
            self.health = self.request("system", "health")
            state = self.health.get("result", {})
            if state.get("pid") != self.pid:
                raise NativeServiceError("Health PID does not match this service process")
            if state.get("ready") is not True or state.get("addon", {}).get("admissible") is not True:
                raise NativeServiceError("Native addon is not ready/admissible")
            if state.get("addon", {}).get("details", {}).get("qtRequired") is not False:
                raise NativeServiceError("Native addon did not confirm Qt UI is unnecessary")
            self.report["health"] = self.health
            new_pids = sorted(set(editor_pids()) - set(self._before))
            if new_pids:
                self.report["new_editor_pids"] = new_pids
                raise NativeServiceError("An editor process appeared; this run is not verified headless")
            return self
        except Exception as exc:
            self.report["exception"] = str(exc)
            try:
                self.close()
            except Exception as cleanup_exc:
                self.report["cleanup_error"] = str(cleanup_exc)
                self._save_report()
            raise

    def request(self, domain, operation, params=None, target=None):
        if not self.process or self.process.poll() is not None or self._closed:
            raise NativeServiceError("Native service is not running")
        if domain not in {"system", "player", "draft", "edit", "job"}:
            raise ValueError("Unsupported native request domain")
        if not isinstance(operation, str) or not operation.strip():
            raise ValueError("Operation must be a nonempty string")
        if params is not None and not isinstance(params, dict):
            raise ValueError("Native request params must be a JSON object")
        if target is not None and (not isinstance(target, str) or not target):
            raise ValueError("Native target must be a nonempty string")
        request = {"id": uuid.uuid4().hex, "protocolVersion": 1,
                   "domain": domain, "operation": operation}
        if params is not None:
            request["params"] = params
        if target is not None:
            request["targetId"] = target
        raw = json.dumps(request, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_JSON_BYTES:
            raise ValueError("Native request exceeds 128 MiB")
        record = {"id": request["id"], "domain": domain, "operation": operation}
        self.report["calls"].append(record)
        try:
            result = subprocess.run(
                [str(self.install / "bun.exe"), str(Path(__file__).with_name("native_rpc_client.mjs")),
                 self.endpoint, str(round(self.request_timeout * 1000))],
                input=raw, capture_output=True, timeout=self.request_timeout + 5,
                env=self.env, cwd=self.scratch, creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode:
                raise NativeServiceError(result.stderr.decode("utf-8", errors="replace")[:1000].strip()
                                         or "Native RPC client failed")
            response = json.loads(result.stdout.decode("utf-8"))
            if not isinstance(response, dict) or response.get("id") != request["id"] or response.get("protocolVersion") != 1:
                raise NativeServiceError("Native response envelope identity mismatch")
            record["ok"] = response.get("ok")
            if response.get("ok") is not True:
                record["error"] = response.get("error")
                raise NativeServiceError("Native request failed: " + json.dumps(response.get("error"), ensure_ascii=False))
            body = response.get("result")
            if isinstance(body, dict):
                record["result_keys"] = list(body)
                if body.get("accepted") is False:
                    record["accepted"] = False
                    record["error"] = body.get("error", body.get("message", "rejected"))
                    raise NativeServiceError("Native operation rejected: " + str(record["error"]))
            return response
        except (OSError, ValueError, subprocess.SubprocessError, NativeServiceError) as exc:
            record["exception"] = str(exc)
            raise NativeServiceError(str(exc)) from exc

    def _save_report(self):
        if self._created_scratch:
            (self.scratch / "service-report.json").write_text(
                json.dumps(self.report, ensure_ascii=False, indent=2), encoding="utf-8")

    def close(self):
        if self._closed:
            return
        cleanup_error = None
        try:
            if self.process and self.process.poll() is None:
                try:
                    self.request("system", "shutdown", {"expectedPid": self.pid})
                    self.report["shutdown_accepted"] = True
                except Exception as exc:
                    self.report["shutdown_error"] = str(exc)
                    cleanup_error = exc
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # Only this subprocess handle; never kill by image name or discovered PID.
                    self.report["own_process_terminated_after_timeout"] = True
                    self.process.terminate()
                    self.process.wait(timeout=5)
            if self.process:
                self.report["service_exit_code"] = self.process.returncode
                if self.process.returncode not in (0, None) and not self.report.get("own_process_terminated_after_timeout"):
                    cleanup_error = cleanup_error or NativeServiceError(
                        f"Native service exited unsuccessfully: {self.process.returncode}")
        except Exception as exc:
            cleanup_error = exc
            self.report["cleanup_error"] = str(exc)
        finally:
            self._closed = True
            for stream in (self._stdout, self._stderr):
                if stream:
                    stream.close()
            if self._created_scratch:
                try:
                    new_pids = sorted(set(editor_pids()) - set(self._before))
                    self.report["new_editor_pids"] = new_pids
                    self.report["no_new_editor_process_observed"] = not new_pids
                    if new_pids:
                        cleanup_error = NativeServiceError("An editor process appeared during the run")
                except Exception as exc:
                    self.report["editor_process_check_error"] = str(exc)
                    cleanup_error = cleanup_error or exc
                self._save_report()
        if cleanup_error:
            raise NativeServiceError(str(cleanup_error)) from cleanup_error


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("health",))
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--out", required=True, help="New isolated output directory; never overwrite")
    args = parser.parse_args()
    try:
        with Service(args.install_dir, args.out) as service:
            pass
        print(json.dumps({"ready": True, "report": str(service.scratch / "service-report.json"),
                          "no_new_editor_process_observed": service.report.get("no_new_editor_process_observed"),
                          "native_editor_acceptance": "not_checked"}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ready": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
