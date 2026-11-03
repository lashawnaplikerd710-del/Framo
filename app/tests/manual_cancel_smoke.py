"""Run a disposable long child command, cancel it, and verify only it stopped."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_core.local_executor import LocalExecutor, ToolCancelled
from agent_core.task_store import TaskStore


def main():
    with TemporaryDirectory(prefix='jianying-agent-cancel-') as temp:
        root = Path(temp)
        scripts = root / 'scripts'; scripts.mkdir()
        (scripts / 'local_asr.py').write_text('''
import os, pathlib, sys, time
out = pathlib.Path(sys.argv[1])
(out.parent / 'child.pid').write_text(str(os.getpid()))
time.sleep(30)
out.mkdir()
(out / 'asr-report.json').write_text('{"status":"local_asr_completed"}')
''', encoding='utf-8')
        store = TaskStore(root / 'state')
        job = store.put_job({'schema_version': '1.0'})
        executor = LocalExecutor(sys.executable, scripts, root, store, job)
        output = store.job_dir(job) / 'cancel-test'
        result = {}

        def work():
            try:
                executor.run('transcribe', 'local_asr.py', [output], output_dir=output,
                             report_file=output / 'asr-report.json', timeout_seconds=40)
            except Exception as exc:
                result['exception'] = type(exc).__name__
                result['message'] = str(exc)

        thread = threading.Thread(target=work)
        thread.start()
        pid_file = output.parent / 'child.pid'
        deadline = time.monotonic() + 10
        while not pid_file.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not pid_file.is_file():
            raise RuntimeError('Disposable child did not start')
        child_pid = int(pid_file.read_text())
        store.request_cancel(job)
        thread.join(15)
        if thread.is_alive() or result.get('exception') != ToolCancelled.__name__:
            raise RuntimeError('Cancellation did not complete safely: ' + repr(result))
        if psutil.pid_exists(child_pid):
            raise RuntimeError('Cancelled operation child process is still running')
        print(json.dumps({'schema_version': '1.0', 'job_id': job,
                          'cancel_result': result['exception'],
                          'child_process_exited': True}, ensure_ascii=False))


if __name__ == '__main__':
    main()
