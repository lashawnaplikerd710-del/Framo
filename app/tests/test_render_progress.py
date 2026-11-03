import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_core.independent_export import render_encoder_threads, run_ffmpeg_progress


class RenderProgressTests(unittest.TestCase):
    def test_encoder_parallelism_is_bounded_even_on_large_hosts(self):
        for cpus,expected in ((None,1),(1,1),(2,1),(4,2),(16,4),(128,4)):
            with patch('agent_core.independent_export.os.cpu_count',return_value=cpus):
                self.assertEqual(render_encoder_threads(),expected)

    def test_clock_updates_come_from_ffmpeg_and_stderr_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'progress.log';events=[];proc=Mock()
            proc.poll.return_value=0;proc.returncode=0
            def finish(**kwargs):
                path.write_text('out_time_us=2000000\nspeed=1.5x\nprogress=end\n')
                return b'',b'volume analysis {"input_i": -18}'
            proc.communicate.side_effect=finish
            with patch('agent_core.independent_export.subprocess.Popen',return_value=proc) as spawn:
                result=run_ffmpeg_progress(['ffmpeg','-i','in','out'],progress_path=path,
                    duration_s=4,timeout_s=10,progress_callback=events.append)
            self.assertIn('-progress',spawn.call_args.args[0])
            self.assertEqual(events[0]['processed_s'],0)
            self.assertEqual(events[-1]['processed_s'],2)
            self.assertEqual(events[-1]['percent'],50) # Never fabricate 100% on process exit.
            self.assertIn('input_i',result['stderr'])

    def test_cancellation_kills_and_reaps_child(self):
        with tempfile.TemporaryDirectory() as folder:
            proc=Mock();proc.poll.return_value=None
            with patch('agent_core.independent_export.subprocess.Popen',return_value=proc):
                with self.assertRaisesRegex(RuntimeError,'任务已取消'):
                    run_ffmpeg_progress(['ffmpeg'],progress_path=Path(folder)/'p',duration_s=5,
                        timeout_s=10,cancelled=lambda:True)
            proc.kill.assert_called_once();proc.communicate.assert_called_once()

    def test_callback_failure_does_not_orphan_encoder(self):
        with tempfile.TemporaryDirectory() as folder:
            proc=Mock();proc.poll.return_value=None
            with patch('agent_core.independent_export.subprocess.Popen',return_value=proc):
                with self.assertRaisesRegex(ValueError,'callback failed'):
                    run_ffmpeg_progress(['ffmpeg'],progress_path=Path(folder)/'p',duration_s=5,
                        timeout_s=10,progress_callback=Mock(side_effect=ValueError('callback failed')))
            proc.kill.assert_called_once();proc.communicate.assert_called_once()


if __name__=='__main__':unittest.main()
