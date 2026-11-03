"""Registry boundary tests with mocked native tools: no services or model calls."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_core.tools import ToolHub


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')


class ToolBoundaryTests(unittest.TestCase):
    def setUp(self):
        # Exercise registry/QA internals with mocked native backends only.
        # Real build entry-point blocking is tested in test_resource_policy.
        guard = patch('agent_core.resource_policy.require_internal_resources')
        guard.start(); self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.package = self.base / 'package'
        (self.package / 'scripts').mkdir(parents=True)
        (self.package / 'scripts/autocut_task.py').write_text('# mocked', encoding='utf-8')
        (self.package / 'references').mkdir()
        (self.package / 'references/native-timeline.md').write_text('known rules', encoding='utf-8')
        self.install = self.base / 'install'
        self.install.mkdir()
        self.source = self.base / 'clip.mp4'
        self.source.write_bytes(b'mocked source')
        self.run = self.base / 'run'
        self.calls = []
        self.hub = self.make_hub()

    def tearDown(self):
        self.temp.cleanup()

    def make_hub(self):
        hub = ToolHub(self.package, self.install, self.run, [self.source], 'local native only')
        hub._call = self.fake_call
        return hub

    def fake_call(self, module, function, *args, **kwargs):
        self.calls.append((module, function, args, kwargs))
        if (module, function) == ('autocut_task', 'initialize'):
            task = Path(args[1])
            write(task / 'inventory.json', {'sources': [{'id': 's001', 'path': str(self.source),
                  'sha256': hashlib.sha256(self.source.read_bytes()).hexdigest(),
                  'probe_status': 'ok', 'metadata': {'has_video': True, 'duration': 2, 'has_audio': True}}]})
            write(task / 'task.json', {'model_runtime': 'current Codex session'})
            return {}
        if (module, function) == ('autocut_task', 'status'):
            return {'state': {'model_runtime': 'current Codex session'}}
        if (module, function) in (('autocut_task', 'sample_source'), ('native_preview', 'preview')):
            output = self.run / 'task/observations/mock' if module == 'autocut_task' else Path(args[2])
            output.mkdir(parents=True)
            frames = []
            for index, when in enumerate(kwargs['positions']):
                path = output / f'{index}.png'
                path.write_bytes(b'mock image bytes')
                frames.append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                               'requested_us': when, 'native_pts_us': when, 'width': 360, 'height': 640})
            return {'status': 'native_frames_captured', 'frames': frames}
        if (module, function) == ('native_timeline', 'load_plan'):
            plan = json.loads(Path(args[0]).read_text(encoding='utf-8'))
            if set(plan) != {'schema', 'name', 'canvas', 'steps'}:
                raise ValueError('Strict native validator rejected unknown fields')
            if any(step.get('action') != 'video.add' for step in plan['steps']):
                raise ValueError('Strict native validator rejected unsupported action')
            return plan
        if (module, function) == ('native_timeline', 'build'):
            output = Path(args[2])
            write(output / 'draft/draft_content.json', {'encrypted': 'mock'})
            write(output / 'draft-snapshot.json', {'duration': 1000000, 'tracks': [], 'materials': {}})
            return {'status': 'native_timeline_created', 'duration_us': 1000000, 'tracks': 1, 'segments': 1}
        if (module, function) == ('draft_quality', 'audit'):
            return {'status': 'structural_clear_with_limits', 'summary': {'error': 0}, 'issues': [],
                    'scope': {'full_quality_accepted': False, 'audio_listened': False}}
        if (module, function) == ('quality_review', 'prepare'):
            output = Path(args[2])
            write(output / 'quality-plan.json', {'schema': 'jianying-quality-plan-v1', 'units': [{'id': 'listening-1'}]})
            write(output / 'review-template.json', {'schema': 'jianying-quality-review-v1', 'reviewer': 'current Codex session', 'evidence': []})
            return {'units': 1}
        if (module, function) == ('quality_review', 'validate'):
            result = {'status': 'partial', 'unverified': ['listening-1']}
            write(Path(args[2]), result)
            return result
        raise AssertionError((module, function))

    def inventory(self):
        result = self.hub.execute('inventory', {})
        self.assertTrue(result['ok'], result)
        return result

    def artifact(self, name='plan.json', path='source:s001', **extra):
        plan = {'schema': 'jianying-native-timeline-v1', 'name': 'Example',
                'canvas': {'width': 1080, 'height': 1920, 'fps': 30},
                'steps': [{'action': 'video.add', 'id': 'clip', 'path': path, 'track': 0,
                           'start_us': 0, 'duration_us': 1000000, 'source_start_us': 0}], **extra}
        result = self.hub.execute('write_artifact', {'name': name, 'content': plan})
        self.assertTrue(result['ok'], result)
        return result

    def built(self):
        self.inventory()
        self.artifact()
        result = self.hub.execute('build_timeline', {'artifact_name': 'plan.json'})
        self.assertTrue(result['ok'], result)
        return result['built_id']

    def test_definitions_are_finite_no_shell_or_registration(self):
        names = {item['function']['name'] for item in self.hub.definitions()}
        self.assertEqual(10, len(names))
        self.assertFalse({'shell', 'register', 'asr', 'network'} & names)
        self.assertTrue(all(item['function']['parameters']['additionalProperties'] is False for item in self.hub.definitions()))

    def test_unknown_name_and_unknown_fields_never_call_backend(self):
        self.assertFalse(self.hub.execute('__class__', {})['ok'])
        self.assertFalse(self.hub.execute('inventory', {'command': 'bad'})['ok'])
        self.assertFalse(self.calls)

    def test_inventory_sanitized_idempotent_and_resume(self):
        result = self.inventory()
        self.assertEqual('source:s001', result['sources'][0]['source_token'])
        self.assertNotIn('path', result['sources'][0])
        self.assertNotIn('Codex', json.dumps(result))
        self.inventory()
        self.hub = self.make_hub()
        self.inventory()
        self.assertEqual(1, sum(function == 'initialize' for _, function, _, _ in self.calls))

    def test_changed_source_and_unknown_source_denied(self):
        self.inventory()
        self.assertFalse(self.hub.execute('sample_frames', {'source_id': 's002', 'times_us': [0]})['ok'])
        self.source.write_bytes(b'changed')
        self.assertFalse(self.hub.execute('sample_frames', {'source_id': 's001', 'times_us': [0]})['ok'])

    def test_sample_returns_provenance_and_rejects_bad_arguments(self):
        self.inventory()
        result = self.hub.execute('sample_frames', {'source_id': 's001', 'times_us': [0, 100000]})
        self.assertTrue(result['ok'], result)
        self.assertEqual(2, len(result['frames']))
        self.assertEqual('s001', result['frames'][0]['source_id'])
        for values in ([True], [-1], list(range(21)), [1, 1], []):
            self.assertFalse(self.hub.execute('sample_frames', {'source_id': 's001', 'times_us': values})['ok'])
        self.assertFalse(self.hub.execute('sample_frames', {'source_id': 's001', 'times_us': [0], 'width': 9999})['ok'])

    def test_reference_cannot_read_arbitrary_files(self):
        self.assertEqual('known rules', self.hub.execute('read_reference', {'name': 'native-timeline.md'})['text'])
        for name in ('../secret.md', str(self.source), 'x.py', 'native-timeline.md/../../secret'):
            self.assertFalse(self.hub.execute('read_reference', {'name': name})['ok'])

    def test_artifact_exclusive_filename_and_json_object(self):
        for name in ('../x.json', 'C:/x.json', 'CON.json', 'x.py'):
            self.assertFalse(self.hub.execute('write_artifact', {'name': name, 'content': {}})['ok'])
        self.assertFalse(self.hub.execute('write_artifact', {'name': 'array.json', 'content': []})['ok'])
        self.assertFalse(self.hub.execute('write_artifact', {'name': 'nan.json', 'content': {'n': float('nan')}})['ok'])
        self.assertTrue(self.hub.execute('write_artifact', {'name': 'data.json', 'content': {}})['ok'])
        self.assertFalse(self.hub.execute('write_artifact', {'name': 'data.json', 'content': {}})['ok'])

    def test_build_resolves_tokens_and_preserves_written_artifact(self):
        built_id = self.built()
        original = json.loads((self.run / 'artifacts/plan.json').read_text(encoding='utf-8'))
        resolved = json.loads((self.run / f'resolved-plans/{built_id}.json').read_text(encoding='utf-8'))
        self.assertEqual('source:s001', original['steps'][0]['path'])
        self.assertEqual(str(self.source), resolved['steps'][0]['path'])
        self.assertTrue((self.run / 'tools-state.json').is_file())

    def test_plan_paths_outside_user_inputs_and_urls_denied(self):
        self.inventory()
        outside = self.base / 'secret.mp4'
        outside.write_bytes(b'secret')
        for index, path in enumerate((str(outside), 'https://example.invalid/a.mp4', 'source:s999', '\\\\host\\share\\a.mp4')):
            self.artifact(f'bad{index}.json', path)
            self.assertFalse(self.hub.execute('build_timeline', {'artifact_name': f'bad{index}.json'})['ok'])
        self.assertFalse(any(function == 'build' for _, function, _, _ in self.calls))

    def test_strict_native_validator_runs_before_build(self):
        self.inventory()
        self.artifact(command='arbitrary')
        self.assertFalse(self.hub.execute('build_timeline', {'artifact_name': 'plan.json'})['ok'])
        self.assertFalse(any(function == 'build' for _, function, _, _ in self.calls))

    def test_tampered_artifact_denied(self):
        self.inventory()
        self.artifact()
        (self.run / 'artifacts/plan.json').write_text('{}', encoding='utf-8')
        self.assertFalse(self.hub.execute('build_timeline', {'artifact_name': 'plan.json'})['ok'])

    def test_preview_and_inspection_use_only_known_build_ids(self):
        self.assertFalse(self.hub.execute('preview_draft', {'built_id': '../other', 'times_us': [0]})['ok'])
        identity = self.built()
        preview = self.hub.execute('preview_draft', {'built_id': identity, 'times_us': [0]})
        self.assertTrue(preview['ok'], preview)
        self.assertEqual(identity, preview['frames'][0]['built_id'])
        result = self.hub.execute('inspect_draft', {'built_id': identity})
        self.assertTrue(result['ok'], result)
        self.assertFalse(result['scope']['full_quality_accepted'])

    def test_changed_draft_denied_before_preview(self):
        identity = self.built()
        (self.run / identity / 'draft/draft_content.json').write_bytes(b'changed')
        result = self.hub.execute('preview_draft', {'built_id': identity, 'times_us': [0]})
        self.assertFalse(result['ok'])
        self.assertFalse(any(module == 'native_preview' for module, _, _, _ in self.calls))

    def test_backend_failure_is_actionable_json_and_no_build_success(self):
        self.inventory()
        self.artifact()
        original = self.hub._call
        def fail(module, function, *args, **kwargs):
            if function == 'build':
                raise RuntimeError('Pinned native version mismatch')
            return original(module, function, *args, **kwargs)
        self.hub._call = fail
        result = self.hub.execute('build_timeline', {'artifact_name': 'plan.json'})
        self.assertFalse(result['ok'])
        self.assertIn('version mismatch', result['error']['message'])
        self.assertEqual([], self.hub.execute('task_status', {})['built_ids'])

    def test_prepare_and_validate_quality_keeps_partial(self):
        identity = self.built()
        quality = self.hub.execute('prepare_quality', {'built_id': identity})
        self.assertTrue(quality['ok'], quality)
        self.assertNotIn('Codex', quality['review_template']['reviewer'])
        self.hub.execute('write_artifact', {'name': 'review.json', 'content': quality['review_template']})
        result = self.hub.execute('validate_quality', {'quality_id': quality['quality_id'], 'report_artifact_name': 'review.json'})
        self.assertTrue(result['ok'], result)
        self.assertEqual('partial', result['status'])

    def test_frame_only_runtime_cannot_attest_continuous_listening(self):
        identity = self.built()
        quality = self.hub.execute('prepare_quality', {'built_id': identity})
        record = quality['review_template']
        record['evidence'] = [{'kind': 'continuous_av', 'actually_reviewed': True}]
        self.hub.execute('write_artifact', {'name': 'false-review.json', 'content': record})
        result = self.hub.execute('validate_quality', {'quality_id': quality['quality_id'], 'report_artifact_name': 'false-review.json'})
        self.assertFalse(result['ok'])
        self.assertIn('sampled images only', result['error']['message'])

    def test_quality_evidence_path_cannot_read_unprovided_file(self):
        identity = self.built()
        quality = self.hub.execute('prepare_quality', {'built_id': identity})
        hidden = self.base / 'not-authorized.txt'
        hidden.write_text('private', encoding='utf-8')
        record = quality['review_template']
        record['evidence'] = [{'artifact': {'path': str(hidden)}}]
        self.hub.execute('write_artifact', {'name': 'outside-review.json', 'content': record})
        self.assertFalse(self.hub.execute('validate_quality', {'quality_id': quality['quality_id'], 'report_artifact_name': 'outside-review.json'})['ok'])

    def test_changed_run_configuration_rejected(self):
        with self.assertRaises(ValueError):
            ToolHub(self.package, self.install, self.run, [self.source], 'different constraint')

    def test_status_never_promotes_counts_to_quality_acceptance(self):
        self.inventory()
        result = self.hub.execute('task_status', {})
        self.assertFalse(result['full_quality_accepted'])
        self.assertNotIn('Codex', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
