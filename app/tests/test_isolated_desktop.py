"""Real Windows lifecycle checks using Python children only. Never starts Jianying."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from agent_core.isolated_desktop import DesktopJob, child_environment
from agent_core.native_connection import process_image, ConnectionFailure


@unittest.skipUnless(os.name=='nt','Windows job/desktop integration')
class DesktopLifecycleTests(unittest.TestCase):
    def test_owned_child_tree_terminates_without_desktop_switch(self):
        with tempfile.TemporaryDirectory() as folder:
            job=DesktopJob()
            try:
                code='import subprocess,sys,time;subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]);time.sleep(30)'
                job.start(Path(sys.executable),['-c',code],child_environment(),folder)
                deadline=time.monotonic()+5
                while True:
                    sample=job.inspect()
                    if len(sample['job_pids'])>=3 or time.monotonic()>deadline:break
                    time.sleep(.1)
                self.assertGreaterEqual(len(sample['job_pids']),2)
                self.assertTrue(sample['input_desktop_unchanged'])
                self.assertEqual(sample['input_window_count'],0)
            finally:report=job.close()
            self.assertTrue(report['owned_processes_ended'])
            self.assertEqual(report['pending_owned_pids'],[])
            self.assertTrue(report['input_desktop_unchanged'])
            self.assertEqual(job.close(),report)

    def test_job_kills_child_when_owner_exits_without_finally(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder)/'owned.json'
            code=('from agent_core.isolated_desktop import DesktopJob,child_environment;'
                  'import os,sys,time,json;from pathlib import Path;'
                  'j=DesktopJob();j.start(Path(sys.executable),["-c","import time;time.sleep(30)"],child_environment(),sys.argv[1]);'
                  'time.sleep(.3);p=j.inspect()["job_pids"];Path(sys.argv[2]).write_text(json.dumps(p));os._exit(0)')
            completed=subprocess.run([sys.executable,'-X','utf8','-c',code,folder,str(output)],
                       cwd=Path(__file__).resolve().parents[1],env=child_environment(),
                       capture_output=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(completed.returncode,0)
            pids=json.loads(output.read_text());self.assertTrue(pids)
            deadline=time.monotonic()+5;alive=list(pids)
            while alive and time.monotonic()<deadline:
                alive=[]
                for pid in pids:
                    try:process_image(pid);alive.append(pid)
                    except ConnectionFailure as exc:self.assertEqual(str(exc),'discovery_stale')
                if alive:time.sleep(.1)
            self.assertFalse(alive)


if __name__=='__main__':unittest.main()
