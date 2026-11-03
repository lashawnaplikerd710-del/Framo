import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_core.background import launch, worker
from agent_core.local_executor import draft_lock
from agent_core.task_store import TaskStore


ROOT = Path(__file__).resolve().parents[1]
JOB = 'job_' + 'a' * 32


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / 'config.json'
        self.config_path.write_text('{}', encoding='utf-8')
        self.config = {'provider': {'api_key_env': 'DEEPSEEK_API_KEY'}}
        store = TaskStore(self.root / 'data')
        class Service:
            def get_job(self, job):
                return {'state': 'WAITING_FOR_INPUT', 'task_spec': {}}
        self.service = Service()
        self.service.store = store

    def tearDown(self):
        self.temp.cleanup()

    def test_active_worker_is_not_launched_again(self):
        with patch('agent_core.background.load_config', return_value=self.config), \
             patch('agent_core.background.TaskService', return_value=self.service), \
             patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'synthetic-not-a-real-key'}), \
             patch('agent_core.background._spawn_detached') as spawn, \
             draft_lock(self.service.store.job_dir(JOB) / '.background-worker.lock'):
            with self.assertRaises(RuntimeError):
                launch(self.config_path, self.root / 'data', JOB)
            spawn.assert_not_called()

    def test_missing_key_never_starts_a_worker(self):
        with patch('agent_core.background.load_config', return_value=self.config), \
             patch('agent_core.background.TaskService', return_value=self.service), \
             patch.dict(os.environ, {}, clear=True), \
             patch('agent_core.background._spawn_detached') as spawn:
            with self.assertRaisesRegex(ValueError, 'API Key'):
                launch(self.config_path, self.root / 'data', JOB)
            spawn.assert_not_called()

    def test_worker_records_exit_and_redacts_error_without_saving_key(self):
        secret = '[REDACTED_CREDENTIAL]'
        wid = 'b' * 32
        with patch('agent_core.background.load_config', return_value=self.config), \
             patch('agent_core.background.TaskService', return_value=self.service), \
             patch.dict(os.environ, {'DEEPSEEK_API_KEY': secret}), \
             patch.object(sys, 'argv', []), \
             patch('agent_core.__main__.main', side_effect=ValueError('failure ' + secret)):
            code = worker(self.config_path, self.root / 'data', JOB, wid)
        text = (self.service.store.job_dir(JOB) / 'workers' / wid / 'worker.json').read_text(encoding='utf-8')
        result = json.loads(text)
        self.assertEqual(code, 2)
        self.assertEqual(result['status'], 'worker_failed')
        self.assertFalse(result['credential_persisted'])
        self.assertIn('[REDACTED]', result['message'])
        self.assertNotIn(secret, text)

    def test_detached_child_outlives_launching_process_and_inherits_key_only_in_memory(self):
        marker = self.root / 'completed.json'
        stdout_path, stderr_path = self.root / 'stdout.log', self.root / 'stderr.log'
        child = ('import os,time,pathlib,json; time.sleep(1); '
                 'pathlib.Path(__import__("sys").argv[1]).write_text(json.dumps({"inherited": '
                 'os.environ.get("DEEPSEEK_API_KEY")=="synthetic-detachment-fixture"}))')
        parent = ('import os,sys; from agent_core.background import _spawn_detached; '
                  'out=open(sys.argv[1],"wb"); err=open(sys.argv[2],"wb"); '
                  'p=_spawn_detached([sys.executable,"-c",sys.argv[3],sys.argv[4]], '
                  'cwd=os.getcwd(),stdout=out,stderr=err,environment=dict(os.environ)); '
                  'print(p.pid); out.close(); err.close()')
        env = dict(os.environ, DEEPSEEK_API_KEY='synthetic-detachment-fixture')
        result = subprocess.run([sys.executable, '-c', parent, str(stdout_path), str(stderr_path),
                                 child, str(marker)], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip().isdigit())
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(marker.exists(), stderr_path.read_text(encoding='utf-8'))
        self.assertTrue(json.loads(marker.read_text())['inherited'])
        self.assertNotIn('synthetic-detachment-fixture', stdout_path.read_text() + stderr_path.read_text())
        # Allow the child interpreter to close its redirected handles on Windows.
        time.sleep(0.2)


if __name__ == '__main__':
    unittest.main()
