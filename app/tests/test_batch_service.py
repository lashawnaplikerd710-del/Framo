import json
from pathlib import Path
import tempfile
import unittest

from agent_core.task_service import TaskService
from agent_core.task_store import digest


class Inspector:
    def inspect(self, path):
        path = Path(path).resolve(strict=True)
        return {'schema_version': '1.0', 'asset_id': 'asset_' + digest(path)[:24],
                'path': str(path), 'sha256': digest(path), 'width': 720, 'height': 1280,
                'video_duration_us': 15_000_000, 'duration_us': 15_000_000,
                'audio_duration_us': 15_000_000, 'has_audio': True,
                'frame_rate_mode': 'CFR', 'rotation': 0}


class BatchServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        scripts = root / 'scripts'; scripts.mkdir()
        for name in ('native_timeline.py', 'native_subtitles.py', 'native_preview.py',
                     'native_register.py', 'local_asr.py', 'autocut_task.py',
                     'draft_quality.py', 'native_packaging.py', 'packaging_assets.py',
                     'native_audio_scan.py'):
            (scripts / name).write_text('# fixture', encoding='utf-8')
        self.service = TaskService({'provider': {'api_key_env': 'KEY', 'model': 'fixture'},
                                    'toolchain_root': str(root), 'install_dir': str(root)},
                                   root / 'data', inspector=Inspector())
        self.assets = []
        for index in range(2):
            path = root / f'source-{index}.mp4'; path.write_bytes(bytes([index + 1]))
            self.assets.append(self.service.register_asset(path))

    def tearDown(self):
        self.temp.cleanup()

    def test_create_batch_persists_children_and_independent_retry(self):
        specs = [{'output_id': 'first', 'instruction': '自定义视觉剪辑',
                  'workflow': 'custom', 'asset_ids': [self.assets[0]]},
                 {'output_id': 'second', 'instruction': '自定义视觉剪辑',
                  'workflow': 'custom', 'asset_ids': [self.assets[1]]}]
        result = self.service.create_batch(specs, gap_us=1_000_000)
        self.assertEqual(len(result['children']), 2)
        status = self.service.batch_status(result['batch_id'])
        self.assertEqual(status['counts']['processing'], 2)
        child = result['children'][0]
        self.service.store.set_state(child['job_id'], 'FAILED', error='fixture')
        retry = self.service.retry_batch_child(result['batch_id'], child['output_id'])
        self.assertEqual(retry['state'], 'CREATED')
        self.assertEqual(self.service.get_job(result['children'][1]['job_id'])['state'], 'CREATED')

    def test_batch_report_is_written(self):
        result = self.service.create_batch([
            {'output_id': 'one', 'instruction': '自定义视觉剪辑', 'workflow': 'custom',
             'asset_ids': [self.assets[0]]}], gap_us=1_000_000)
        status = self.service.batch_status(result['batch_id'])
        report = Path(status['report_path'])
        self.assertTrue(report.is_file())
        self.assertEqual(json.loads(report.read_text(encoding='utf-8'))['batch_id'], result['batch_id'])

    def test_finalization_refreshes_when_later_child_finishes(self):
        specs = [{'output_id': 'first', 'instruction': '自定义视觉剪辑',
                  'workflow': 'custom', 'asset_ids': [self.assets[0]]},
                 {'output_id': 'second', 'instruction': '自定义视觉剪辑',
                  'workflow': 'custom', 'asset_ids': [self.assets[1]]}]
        result = self.service.create_batch(specs)
        source = Path(self.temp.name) / 'source-0.mp4'
        for index, child in enumerate(result['children']):
            if index == 1:
                break
            job_dir = self.service.store.job_dir(child['job_id'])
            draft = job_dir / 'fixture-draft'
            draft.mkdir()
            snapshot = {
                'id': f'fixture-{index}', 'name': f'Fixture {index}',
                'duration': 3_000_000, 'fps': 30,
                'canvas_config': {'width': 720, 'height': 1280},
                'materials': {'videos': [{'id': f'material-{index}', 'path': str(source)}]},
                'tracks': [{'id': f'track-{index}', 'type': 'video', 'segments': [{
                    'id': f'segment-{index}', 'material_id': f'material-{index}',
                    'target_timerange': {'start': 0, 'duration': 3_000_000},
                    'source_timerange': {'start': 0, 'duration': 3_000_000}}]}],
            }
            (draft / 'draft_content.json').write_text(json.dumps(snapshot), encoding='utf-8')
            delivery = job_dir / 'delivery.json'
            delivery.write_text(json.dumps({'draft_path': str(draft)}), encoding='utf-8')
            self.service.store.put_artifact(child['job_id'], 'delivery', 'revision_001', delivery)
            self.service.store.set_state(child['job_id'], 'READY_FOR_REVIEW')
        first = self.service.finalize_batch(result['batch_id'])
        self.assertEqual(first['validation']['output_count'], 1)
        self.assertEqual(self.service.store.get_batch(result['batch_id'])['state'], 'PROCESSING')
        child = result['children'][1]
        job_dir = self.service.store.job_dir(child['job_id'])
        draft = job_dir / 'fixture-draft'
        draft.mkdir()
        snapshot = {
            'id': 'fixture-second', 'name': 'Fixture second',
            'duration': 3_000_000, 'fps': 30,
            'canvas_config': {'width': 720, 'height': 1280},
            'materials': {'videos': [{'id': 'material-second', 'path': str(source)}]},
            'tracks': [{'id': 'track-second', 'type': 'video', 'segments': [{
                'id': 'segment-second', 'material_id': 'material-second',
                'target_timerange': {'start': 0, 'duration': 3_000_000},
                'source_timerange': {'start': 0, 'duration': 3_000_000}}]}],
        }
        (draft / 'draft_content.json').write_text(json.dumps(snapshot), encoding='utf-8')
        delivery = job_dir / 'delivery.json'
        delivery.write_text(json.dumps({'draft_path': str(draft)}), encoding='utf-8')
        self.service.store.put_artifact(child['job_id'], 'delivery', 'revision_001', delivery)
        self.service.store.set_state(child['job_id'], 'READY_FOR_REVIEW')
        second = self.service.finalize_batch(result['batch_id'])
        self.assertEqual(second['validation']['output_count'], 2)
        self.assertNotEqual(first['input_fingerprint'], second['input_fingerprint'])
        self.assertEqual(self.service.store.get_batch(result['batch_id'])['state'], 'READY_FOR_REVIEW')

    def test_export_before_children_finish_is_reported_without_missing_file_error(self):
        result = self.service.create_batch([{
            'output_id': 'one', 'instruction': '自定义视觉剪辑', 'workflow': 'custom',
            'asset_ids': [self.assets[0]]}])
        report = self.service.export_batch(result['batch_id'])
        self.assertEqual(report['status'], 'waiting_children')


if __name__ == '__main__':
    unittest.main()
