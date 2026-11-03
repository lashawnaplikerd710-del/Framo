"""Fixed local CLI adapters. No model-supplied command line is ever executed."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .task_store import atomic_json, digest


class ToolFailed(RuntimeError):
    pass


class ToolCancelled(RuntimeError):
    pass


class RecoveryRequired(RuntimeError):
    pass


class MediaInspector:
    def inspect(self, path):
        from pymediainfo import MediaInfo
        source = Path(path).resolve(strict=True)
        if not source.is_file() or str(source).startswith(('\\\\', '//')):
            raise ValueError('Choose a regular local media file')
        before = digest(source)
        tracks = MediaInfo.parse(str(source)).tracks
        video = next((track for track in tracks if track.track_type == 'Video'), None)
        audio = next((track for track in tracks if track.track_type == 'Audio'), None)
        if video is None or not video.duration or not video.width or not video.height:
            raise ValueError('Source video stream metadata is unavailable')
        duration_us = round(float(video.duration) * 1000)
        audio_us = round(float(audio.duration) * 1000) if audio and audio.duration else None
        if digest(source) != before:
            raise ValueError('Source changed during registration')
        identity = hashlib.sha256((str(source).casefold() + '\0' + before).encode('utf-8')).hexdigest()[:24]
        return {'schema_version': '1.0', 'asset_id': 'asset_' + identity,
                'path': str(source), 'sha256': before, 'bytes': source.stat().st_size,
                'width': int(video.width), 'height': int(video.height),
                'duration_us': duration_us, 'video_duration_us': duration_us,
                'audio_duration_us': audio_us, 'has_audio': audio is not None,
                'rotation': float(video.rotation or 0),
                'frame_rate': video.frame_rate, 'frame_rate_mode': video.frame_rate_mode or 'unknown',
                'video_codec': video.format, 'audio_sample_rate': int(audio.sampling_rate) if audio and audio.sampling_rate else None}


@contextmanager
def draft_lock(path):
    import msvcrt
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('This job draft is already being written by another process') from None
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


class LocalExecutor:
    def __init__(self, python, scripts, install, store, job_id):
        self.python = Path(python).resolve(strict=True)
        self.scripts = Path(scripts).resolve(strict=True)
        self.install = Path(install).resolve(strict=True)
        self.store = store
        self.job_id = job_id
        self.folder = store.job_dir(job_id)
        self.allowed = {'local_asr.py', 'autocut_task.py', 'native_timeline.py',
                        'native_subtitles.py', 'native_preview.py', 'native_register.py',
                        'native_service.py', 'native_packaging.py', 'native_audio_scan.py', 'native_motion.py',
                        'native_internal_packaging.py', 'native_review_register.py'}

    def _cancelled(self):
        return self.store.get_job(self.job_id)['cancel_requested']

    @contextmanager
    def _native_stage_lock(self, script):
        """Serialize the isolated Jianying host across concurrent jobs."""
        if script not in ('native_internal_packaging.py', 'native_review_register.py'):
            yield
            return
        path = self.store.root / '.native-stage.lock'
        path.parent.mkdir(parents=True, exist_ok=True)
        import msvcrt
        with path.open('a+b') as stream:
            waiting = False
            resume_state = None
            while True:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if self._cancelled():
                        raise ToolCancelled(f'{script} cancelled while waiting for the Jianying host')
                    if not waiting:
                        waiting = True
                        try:
                            current = self.store.get_job(self.job_id)
                            resume_state = current.get('state')
                            self.store.set_state(self.job_id, 'WAITING_FOR_RESOURCE',
                                message='剪映原生宿主正被其他任务使用，当前任务保持排队')
                        except Exception:
                            # A status update must never break the lock queue.
                            waiting = False
                    time.sleep(0.3)
            try:
                if waiting and resume_state:
                    try:
                        self.store.set_state(self.job_id, resume_state,
                            message='剪映原生宿主已空闲，继续执行当前检查点')
                    except Exception:
                        pass
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

    def run(self, stage, script, args, *, output_dir, report_file, timeout_seconds=1800,
            resource_verified=False):
        if script not in self.allowed:
            raise ValueError('Script is not an approved local adapter')
        if script == 'native_packaging.py' or (script == 'native_register.py' and
                                               not resource_verified):
            from .resource_policy import require_internal_resources
            require_internal_resources(script)
        output_dir = Path(output_dir).resolve()
        report_file = Path(report_file).resolve()
        if not output_dir.is_relative_to(self.folder) or not report_file.is_relative_to(output_dir):
            raise ValueError('Native output must belong to this job')
        if self._cancelled():
            raise ToolCancelled('Job cancellation requested')
        script_path = (self.scripts / script).resolve(strict=True)
        if script_path.parent != self.scripts:
            raise ValueError('Approved tool path changed')
        arguments = [str(self.python), '-X', 'utf8', str(script_path), *map(str, args)]
        input_bindings = []
        for arg in args:
            path = Path(str(arg))
            if path.resolve()==output_dir or path.resolve()==report_file:
                continue  # Outputs must never become new inputs during recovery.
            if path.is_file():
                input_bindings.append((str(path.resolve()), digest(path)))
            elif path.is_dir() and (path / 'draft_content.json').is_file():
                input_bindings.append((str(path.resolve()), digest(path / 'draft_content.json')))
            elif path.is_dir() and (path / 'packaging-report.json').is_file():
                input_bindings.append((str(path.resolve()), digest(path / 'packaging-report.json')))
        fingerprint = hashlib.sha256(json.dumps([stage, script, args, input_bindings],
                            ensure_ascii=False, default=str).encode('utf-8')).hexdigest()
        resumable_internal = script == 'native_internal_packaging.py'
        resumable_registration = script == 'native_review_register.py'
        if resumable_registration and output_dir.exists():
            prior=self.store.get_operation(self.job_id,stage,fingerprint)
            if prior is None:raise RecoveryRequired('Registration output has no matching operation fingerprint')
            if prior['status']=='completed':
                from .native_review_registration import validate_registered
                try:return validate_registered(output_dir)
                except (ValueError,OSError,KeyError):
                    raise RecoveryRequired('Registered review draft or homepage entry changed') from None
        if resumable_internal and output_dir.exists():
            prior = self.store.get_operation(self.job_id, stage, fingerprint)
            if prior is None:
                raise RecoveryRequired('Internal packaging output has no matching operation fingerprint')
            if prior['status']=='completed':
                from .native_packaging import validate_completed
                try:return validate_completed(output_dir)
                except (ValueError,OSError,KeyError):
                    raise RecoveryRequired('Completed internal packaging failed its read-only evidence validation') from None
            # The adapter rechecks media and checkpoint hashes even on completed
            # jobs; report existence alone never authorizes result reuse.
        if report_file.is_file() and not resumable_internal and not resumable_registration:
            # A native operation may have completed just before our checkpoint.
            # Read and verify its actual report instead of running it twice.
            result = json.loads(report_file.read_text(encoding='utf-8-sig'))
            prior = self.store.get_operation(self.job_id, stage, fingerprint)
            if prior is None or prior['status'] not in ('running', 'completed'):
                raise RecoveryRequired(f'{stage} report has no matching prior operation fingerprint')
            if result.get('status') in ('native_timeline_created', 'native_subtitles_created',
                                        'native_frames_captured', 'local_asr_completed',
                                        'native_draft_registered', 'native_packaging_created', 'native_motion_created', 'signal_sampling_complete',
                                        'signal_sampling_complete_with_review_flags'):
                self.store.operation(self.job_id, stage, fingerprint, 'completed', report_file)
                return result
            raise RecoveryRequired(f'Existing {stage} report is not a verified success')
        if output_dir.exists() and not resumable_internal and not resumable_registration:
            raise RecoveryRequired(f'{stage} has partial output; inspect it before retrying to avoid duplicate edits')
        operation_id = self.store.operation(self.job_id, stage, fingerprint, 'running')
        log_dir = self.folder / 'operations'; log_dir.mkdir(exist_ok=True)
        stdout_path = log_dir / f'{operation_id}.stdout.log'
        stderr_path = log_dir / f'{operation_id}.stderr.log'
        environment = {k: v for k, v in os.environ.items()
                       if not k.upper().endswith(('API_KEY', '_TOKEN'))}
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
        started = time.monotonic()
        with self._native_stage_lock(script), draft_lock(self.folder / '.draft-write.lock'), stdout_path.open('wb') as stdout, stderr_path.open('wb') as stderr:
            process = subprocess.Popen(arguments, cwd=self.folder, stdin=subprocess.DEVNULL,
                                       stdout=stdout, stderr=stderr, env=environment,
                                       creationflags=flags)
            while process.poll() is None:
                if self._cancelled() or time.monotonic() - started > timeout_seconds:
                    if sys.platform == 'win32':
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                       capture_output=True, timeout=15, creationflags=flags)
                    else:
                        process.kill()
                    process.wait(timeout=15)
                    self.store.operation(self.job_id, stage, fingerprint, 'cancelled' if self._cancelled() else 'timed_out')
                    if self._cancelled():
                        raise ToolCancelled(f'{stage} cancelled; only this operation tree was stopped')
                    raise ToolFailed(f'{stage} exceeded {timeout_seconds}s')
                time.sleep(0.3)
        report = {'schema_version': '1.0', 'operation_id': operation_id, 'stage': stage,
                  'input_sha256': fingerprint, 'exit_code': process.returncode,
                  'duration_seconds': round(time.monotonic() - started, 3),
                  'stdout_log': str(stdout_path), 'stderr_log': str(stderr_path),
                  'report_path': str(report_file), 'report_sha256': digest(report_file) if report_file.is_file() else None}
        atomic_json(log_dir / f'{operation_id}.json', report)
        if process.returncode != 0 or not report_file.is_file():
            self.store.operation(self.job_id, stage, fingerprint, 'failed',
                                 report_file if report_file.is_file() else None)
            raise ToolFailed(f'{stage} failed with exit code {process.returncode}; inspect operation logs')
        result = json.loads(report_file.read_text(encoding='utf-8-sig'))
        if result.get('status') not in ('native_timeline_created', 'native_subtitles_created',
                                       'native_frames_captured', 'local_asr_completed',
                                       'native_draft_registered', 'native_packaging_created',
                                       'native_packaging_ready_for_review', 'native_motion_created',
                                       'signal_sampling_complete',
                                       'signal_sampling_complete_with_review_flags'):
            self.store.operation(self.job_id, stage, fingerprint, 'failed', report_file)
            raise ToolFailed(f'{stage} did not report a verified success state')
        self.store.operation(self.job_id, stage, fingerprint, 'completed', report_file)
        return result


class JianyingAdapter:
    def __init__(self, executor: LocalExecutor):
        self.executor = executor

    def add_motion(self, draft_dir, request_file, output):
        output = Path(output)
        return self.executor.run('motion', 'native_motion.py',
            ['--install-dir', str(self.executor.install), '--draft', str(draft_dir),
             '--request', str(request_file), '--out', str(output)], output_dir=output,
            report_file=output/'native-motion-report.json')

    def build(self, plan_file, output):
        return self.executor.run('build_draft', 'native_timeline.py',
            ['--plan', plan_file, '--install-dir', self.executor.install, '--out', output],
            output_dir=output, report_file=Path(output) / 'native-timeline-report.json')

    def package(self, draft_dir, request_file, output):
        return self.executor.run('package_draft', 'native_packaging.py',
            ['--install-dir', self.executor.install, '--draft', draft_dir,
             '--request', request_file, '--out', output],
            output_dir=output, report_file=Path(output) / 'packaging-report.json', timeout_seconds=900)

    def audio_scan(self, draft_dir, output):
        return self.executor.run('audio_signal_scan', 'native_audio_scan.py',
            ['--install-dir', self.executor.install, '--draft', draft_dir, '--out', output],
            output_dir=output, report_file=Path(output)/'audio-scan-report.json', timeout_seconds=900)

    def package_internal(self, draft_dir, request_file, output, *, allow_isolated_host=False):
        if allow_isolated_host is not True:
            raise ValueError('Internal resources need explicit isolated-host authorization')
        return self.executor.run('package_internal', 'native_internal_packaging.py',
            ['--install-dir', self.executor.install, '--draft', draft_dir,
             '--request', request_file, '--out', output, '--isolated-host'], output_dir=output,
            report_file=Path(output)/'packaging-report.json', timeout_seconds=1800)

    def add_subtitles(self, draft_dir, cues_file, output, provider='local_sensevoice_unproofread'):
        return self.executor.run('add_subtitles', 'native_subtitles.py',
            ['--install-dir', self.executor.install, '--draft', draft_dir,
             '--captions', cues_file, '--out', output, '--provider', provider],
            output_dir=output, report_file=Path(output) / 'subtitle-report.json')

    def preview(self, draft_dir, output, times_us):
        args = ['--install-dir', self.executor.install, '--draft', draft_dir, '--out', output,
                '--width', 540]
        for value in times_us:
            args.extend(['--time-us', value])
        return self.executor.run('render_preview', 'native_preview.py', args,
            output_dir=output, report_file=Path(output) / 'preview-report.json', timeout_seconds=600)

    def publish(self, draft_dir, draft_root, user_data, name, output, *, resource_verified=False):
        return self.executor.run('register_draft', 'native_register.py',
            ['--install-dir', self.executor.install, '--draft', draft_dir,
             '--draft-root', draft_root, '--user-data', user_data,
             '--name', name, '--out', output], output_dir=output,
            report_file=Path(output) / 'registration-report.json', timeout_seconds=900,
            resource_verified=resource_verified)

    def register_review(self, packaging_dir, draft_root, user_data, name, output):
        return self.executor.run('register_native_review','native_review_register.py',
            ['--install-dir',self.executor.install,'--packaging',packaging_dir,'--draft-root',draft_root,
             '--user-data',user_data,'--name',name,'--out',output,'--isolated-host'],output_dir=output,
            report_file=Path(output)/'registration-report.json',timeout_seconds=900)
