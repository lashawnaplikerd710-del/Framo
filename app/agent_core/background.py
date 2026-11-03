"""Detached worker. Can load the OS credential; never persists keys in job logs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .local_executor import draft_lock
from .provider import _redact_error_text
from .runtime import load_config
from .task_service import TaskService
from .task_store import atomic_json, stamp

ROOT = Path(__file__).resolve().parents[1]


def _spawn_detached(command, *, cwd, stdout, stderr, environment):
    options = {'stdin': subprocess.DEVNULL, 'stdout': stdout, 'stderr': stderr,
               'cwd': str(cwd), 'env': environment, 'close_fds': True}
    if sys.platform == 'win32':
        options['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options['start_new_session'] = True
    return subprocess.Popen(command, **options)


def launch(config_path, state_dir, job_id):
    from .credential_store import credential_environment
    config = load_config(config_path)
    with credential_environment(config['provider']):
        return _launch(config_path, state_dir, job_id)


def _launch(config_path, state_dir, job_id):
    config_path = Path(config_path).resolve(strict=True)
    state_dir = Path(state_dir).resolve()
    config = load_config(config_path)
    service = TaskService(config, state_dir)
    job = service.get_job(job_id)
    if job['state'] in ('COMPLETED', 'READY_FOR_REVIEW'):
        raise ValueError('This job is already ready for review; inspect or publish it instead')
    binding = job['task_spec'].get('runtime_binding')
    if binding is not None and binding != service._runtime_binding():
        raise ValueError('Runtime differs from the saved task; do not resume with different tools')
    if not os.environ.get(config['provider']['api_key_env'], '').strip():
        raise ValueError('Enter the API Key at the local hidden prompt before starting a worker')
    folder = service.store.job_dir(job_id)
    # The launch lock serializes start requests but is never acquired by the
    # child. Release the worker/job probes before spawning to avoid a handoff race.
    with draft_lock(folder / '.background-launch.lock'):
        with draft_lock(folder / '.background-worker.lock'), draft_lock(folder / '.job-run.lock'):
            pass
        worker_id = uuid.uuid4().hex
        output = folder / 'workers' / worker_id
        output.mkdir(parents=True, exist_ok=False)
        command = [sys.executable, '-X', 'utf8', '-m', 'agent_core.background',
                   '--config', str(config_path), '--state-dir', str(state_dir),
                   '--job', job_id, '--worker-id', worker_id]
        with (output / 'stdout.log').open('wb') as stdout, (output / 'stderr.log').open('wb') as stderr:
            process = _spawn_detached(command, cwd=ROOT, stdout=stdout, stderr=stderr,
                                      environment=dict(os.environ))
        receipt = {'schema_version': '1.0', 'job_id': job_id, 'worker_id': worker_id,
                   'pid': process.pid, 'started_at': stamp(), 'status': 'background_launch_accepted',
                   'worker_report': str(output / 'worker.json'),
                   'stdout_log': str(output / 'stdout.log'), 'stderr_log': str(output / 'stderr.log'),
                   'credential_persisted': False}
        atomic_json(folder / 'background-latest.json', receipt)
        # Confirm startup while retaining only the launch lock. No model request
        # is awaited; the parent may exit while the detached child keeps working.
        for _ in range(50):
            if (output / 'worker.json').exists():
                report = json.loads((output / 'worker.json').read_text(encoding='utf-8'))
                if report['status'] == 'worker_failed':
                    raise RuntimeError('Background worker failed; inspect its saved report')
                break
            if process.poll() is not None:
                raise RuntimeError('Background worker exited before startup; inspect its logs')
            time.sleep(0.1)
    return receipt


def worker(config_path, state_dir, job_id, worker_id):
    if len(worker_id) != 32 or any(char not in '0123456789abcdef' for char in worker_id):
        raise ValueError('Invalid worker identity')
    config = load_config(config_path)
    service = TaskService(config, state_dir)
    folder = service.store.job_dir(job_id)
    report_path = folder / 'workers' / worker_id / 'worker.json'
    report = {'schema_version': '1.0', 'job_id': job_id, 'worker_id': worker_id,
              'pid': os.getpid(), 'started_at': stamp(), 'status': 'running',
              'credential_persisted': False}
    with draft_lock(folder / '.background-worker.lock'):
        atomic_json(report_path, report)
        code = 2
        try:
            from .__main__ import main as cli_main
            sys.argv = ['agent', '--config', str(config_path), '--state-dir', str(state_dir),
                        'resume', '--job', job_id]
            code = cli_main()
            report['status'] = 'worker_finished' if code == 0 else 'worker_failed'
            report['job_state'] = service.get_job(job_id)['state']
        except BaseException as exc:
            report['status'] = 'worker_failed'
            report['error_type'] = type(exc).__name__
            report['message'] = _redact_error_text(str(exc),
                os.environ.get(config['provider']['api_key_env'], ''), 400)
        finally:
            report.update(exit_code=code, finished_at=stamp())
            atomic_json(report_path, report)
        return code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--job', required=True)
    parser.add_argument('--worker-id', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_id:
        return worker(args.config, args.state_dir, args.job, args.worker_id)
    result = launch(args.config, args.state_dir, args.job)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
