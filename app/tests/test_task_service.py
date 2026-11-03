import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_core.task_service import TaskService, task_spec
from agent_core.task_store import atomic_json, digest


class FakeInspector:
    def inspect(self, path):
        path = Path(path).resolve(strict=True)
        return {'schema_version': '1.0', 'asset_id': 'asset_' + 'a' * 24,
                'path': str(path), 'sha256': digest(path), 'width': 720, 'height': 1280,
                'video_duration_us': 10_000_000, 'frame_rate_mode': 'CFR'}


class TaskServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        scripts = self.root / 'scripts'; scripts.mkdir()
        for name in ('native_timeline.py', 'native_subtitles.py', 'native_preview.py',
                     'native_register.py',
                     'local_asr.py', 'autocut_task.py', 'draft_quality.py', 'native_packaging.py', 'packaging_assets.py', 'native_audio_scan.py'):
            (scripts / name).write_text('# fixture', encoding='utf-8')
        self.config = {'provider': {'api_key_env': 'DEEPSEEK_API_KEY', 'model': 'deepseek-flash'},
                       'toolchain_root': str(self.root), 'install_dir': str(self.root)}
        self.service = TaskService(self.config, self.root / 'data', inspector=FakeInspector())

    def tearDown(self):
        self.temp.cleanup()

    def test_asset_job_artifact_events_and_cancel(self):
        source = self.root / '素材 空格.mp4'; source.write_bytes(b'media')
        asset_id = self.service.register_asset(source)
        job_id = self.service.create_job(task_spec('剪成60秒竖屏，添加中文字幕', [asset_id], 'interview'))
        job = self.service.get_job(job_id)
        self.assertEqual(job['task_spec']['target']['duration_ms'], 60000)
        self.assertEqual(job['task_spec']['target']['aspect_ratio'], '9:16')
        report = self.service.store.job_dir(job_id) / 'example.json'
        atomic_json(report, {'schema_version': '1.0', 'value': 1})
        artifact = self.service.store.put_artifact(job_id, 'example', 'revision_001', report, verified=True)
        self.assertTrue(self.service.get_artifact(artifact['artifact_id'])['verified'])
        events = self.service.read_events(job_id, 0)
        self.assertEqual([event['sequence'] for event in self.service.read_events(job_id, events[0]['sequence'])],
                         [event['sequence'] for event in events[1:]])
        self.service.cancel_job(job_id)
        self.assertTrue(self.service.get_job(job_id)['cancel_requested'])
        report.write_text('{"changed":true}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.service.get_artifact(artifact['artifact_id'])

    def test_direct_frame_extraction_retries_and_uses_accurate_seek(self):
        output = self.root / 'frame.png'
        calls = []

        class Result:
            def __init__(self, code, stderr=''):
                self.returncode = code
                self.stderr = stderr

        def run(command, **kwargs):
            calls.append(command)
            target = Path(command[-1])
            if len(calls) == 1:
                return Result(0, '')  # transient: success without a frame
            target.write_bytes(b'PNG evidence')
            return Result(0, '')

        with patch('agent_core.task_service.subprocess.run', side_effect=run):
            self.service._extract_direct_frame('ffmpeg.exe', 'source.mp4', 18_619_999, output)
        self.assertTrue(output.is_file())
        self.assertEqual(len(calls), 2)
        self.assertLess(calls[1].index('-i'), calls[1].index('-ss'))

    def test_conflicting_or_unsupported_specs_fail_before_job(self):
        source = self.root / '素材.mp4'; source.write_bytes(b'media')
        asset_id = self.service.register_asset(source)
        with self.assertRaisesRegex(ValueError, 'draft'):
            self.service.create_job(task_spec('导出MP4成片', [asset_id]))
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            task_spec('剪辑', [])
        with self.assertRaisesRegex(ValueError, 'workflow'):
            task_spec('剪辑', [asset_id], 'vlog')

    def test_explicit_speech_constraints_and_original_order(self):
        spec = task_spec('剪成60秒，按原顺序，必须保留“关键观点”，必须删除“口误”，删除重复停顿',
                         ['asset_' + 'a' * 24])
        self.assertEqual(spec['constraints']['must_keep'], ['关键观点'])
        self.assertEqual(spec['constraints']['must_remove'], ['口误'])
        self.assertFalse(spec['constraints']['allow_reorder'])
        with self.assertRaisesRegex(ValueError, 'both kept and removed'):
            task_spec('必须保留“甲”，必须删除“甲”', ['asset_' + 'a' * 24])

    def test_product_ad_always_uses_new_native_narration(self):
        self.service.config['narration']={'product_ad_default':'new_native'}
        ids=[]
        for i,letter in enumerate(('a','b')):
            file=self.root/f'voice-source-{i}.mp4';file.write_bytes(bytes([i+1]))
            value=self.service.inspector.inspect(file);value['asset_id']='asset_'+letter*24
            ids.append(self.service.store.put_asset(value))
        job=self.service.create_job(task_spec('混剪带货草稿',ids,'product_ad'))
        spec=self.service.get_job(job)['task_spec']
        self.assertEqual(spec['narration_mode'],'new_native');self.assertTrue(spec['separate_narration'])
        self.assertTrue(spec['mute_source_audio'])
        keep=self.service.create_job(task_spec('混剪带货草稿，保留原声',ids,'product_ad'))
        self.assertEqual(self.service.get_job(keep)['task_spec']['narration_mode'],'new_native')
        self.assertFalse(self.service.get_job(keep)['task_spec']['mute_source_audio'])

    def test_io_repair_preserves_checked_stages_but_rejects_editing_code_changes(self):
        source=self.root/'io-upgrade.mp4';source.write_bytes(b'media')
        aid=self.service.register_asset(source);job=self.service.create_job(task_spec('剪辑',[aid]))
        self.service.store.set_state(job,'FAILED',message='IO failure fixture')
        report=self.service.store.job_dir(job)/'model-review.json';report.write_text('{"decision":"revise"}')
        self.service.store.put_artifact(job,'model_review','revision_001',report,verified=True)
        binding=self.service._runtime_binding()
        changed={**binding,'core_sha256':{**binding['core_sha256'],'native_review_registration.py':'io-fix'}}
        with patch.object(self.service,'_runtime_binding',return_value=changed):
            self.service.upgrade_failed_job_runtime(job,io_only=True)
        self.assertTrue(report.is_file())
        self.assertIsNotNone(self.service.store.artifact(job,'model_review','revision_001'))
        editing={**changed,'core_sha256':{**changed['core_sha256'],'commerce.py':'edit-change'}}
        with patch.object(self.service,'_runtime_binding',return_value=editing):
            with self.assertRaisesRegex(ValueError,'IO-only'):
                self.service.upgrade_failed_job_runtime(job,io_only=True)

    def test_narration_repair_requires_unfinished_synthesis_and_keeps_base_review(self):
        source=self.root/'voice-upgrade.mp4';source.write_bytes(b'media')
        aid=self.service.register_asset(source);job=self.service.create_job(task_spec('剪辑',[aid]))
        self.service.store.set_state(job,'FAILED',message='Caption layout fixture')
        folder=self.service.store.job_dir(job);binding=self.service._runtime_binding()
        changed={**binding,'core_sha256':{**binding['core_sha256'],'native_narration.py':'caption-fix'}}
        with patch.object(self.service,'_runtime_binding',return_value=changed):
            with self.assertRaisesRegex(ValueError,'unfinished'):
                self.service.upgrade_failed_job_runtime(job,narration_only=True)
            voice=folder/'voice-revision';voice.mkdir();(voice/'tts-receipts.json').write_text('[]')
            report=folder/'base-review.json';report.write_text('{"decision":"revise"}')
            self.service.store.put_artifact(job,'model_review','revision_003',report,verified=True)
            self.service.upgrade_failed_job_runtime(job,narration_only=True)
            self.assertTrue(report.is_file())
            (voice/'narration-report.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'unfinished'):
                self.service.upgrade_failed_job_runtime(job,narration_only=True)

    def test_code_upgrade_retains_checkpoints_but_rejects_changed_source_and_model(self):
        source=self.root/'upgrade.mp4';source.write_bytes(b'media')
        aid=self.service.register_asset(source)
        job=self.service.create_job(task_spec('剪辑',[aid]))
        with self.assertRaisesRegex(ValueError,'failed jobs'):self.service.upgrade_failed_job_runtime(job)
        self.service.store.set_state(job,'FAILED',message='fixture')
        binding=self.service._runtime_binding()
        changed={**binding,'core_sha256':{**binding['core_sha256'],'native_packaging.py':'new-code'}}
        with patch.object(self.service,'_runtime_binding',return_value=changed):
            migrated=self.service.upgrade_failed_job_runtime(job)
        self.assertEqual(migrated['state'],'FAILED')
        self.assertEqual(migrated['task_spec']['runtime_binding'],changed)
        self.assertTrue(list((self.service.store.job_dir(job)/'runtime-upgrades').glob('*.json')))
        with patch.object(self.service,'_runtime_binding',return_value={**changed,'model':'other-model'}):
            with self.assertRaisesRegex(ValueError,'new task'):self.service.upgrade_failed_job_runtime(job)
        source.write_bytes(b'changed')
        with patch.object(self.service,'_runtime_binding',return_value=changed):
            with self.assertRaisesRegex(ValueError,'source changed'):self.service.upgrade_failed_job_runtime(job)
