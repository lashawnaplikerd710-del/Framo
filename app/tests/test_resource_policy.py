import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_core.resource_policy import POLICY, ResourcePolicyBlocked, bind_policy, inspect_local_catalog
from agent_core.runtime import run
from agent_core.task_service import task_spec
import test_task_service


class ResourcePolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_task_service.TaskServiceTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service = self.fixture.service
        self.source = self.fixture.root / 'source.mp4'; self.source.write_bytes(b'user media')
        self.asset_id = self.service.register_asset(self.source)

    def test_every_workflow_binds_policy_and_rejects_external_override(self):
        for workflow in ('talking_head', 'interview', 'product_ad', 'product_montage'):
            spec = task_spec('自主剪辑', ['a', 'b'] if workflow.startswith('product') else ['a'], workflow)
            self.assertEqual(spec['resource_policy'], POLICY)
        old = task_spec('剪辑', [self.asset_id]); old.pop('resource_policy')
        job = self.service.create_job(old)
        self.assertEqual(self.service.get_job(job)['task_spec']['resource_policy'], POLICY)
        with self.assertRaises(ValueError):
            bind_policy({**old, 'resource_policy': 'allow_external'})

    def test_preflight_blocks_before_model_executor_or_draft(self):
        provider = Mock(side_effect=AssertionError('No model should be instantiated'))
        executor = Mock(side_effect=AssertionError('No executor should be instantiated'))
        self.service.provider_factory = provider
        self.service.executor_factory = executor
        job = self.service.create_job(task_spec('自主剪辑', [self.asset_id]))
        result = self.service.start_job(job)
        self.assertEqual(result['state'], 'BLOCKED_RESOURCES')
        provider.assert_not_called(); executor.assert_not_called()
        artifacts = self.service.list_artifacts(job)
        self.assertEqual([r['kind'] for r in artifacts], ['resource_preflight'])
        preflight = json.loads((self.service.store.job_dir(job) / 'resource-preflight.json').read_text(encoding='utf-8'))
        self.assertIn('native_connection', preflight)
        self.assertFalse(preflight['native_connection']['gui_started'])
        self.assertFalse(preflight['automatic_retry'])
        self.assertFalse(list(self.service.store.job_dir(job).rglob('draft_content.json')))
        self.assertEqual(self.source.read_bytes(), b'user media')
        self.assertEqual(self.service.resume_job(job)['state'], 'BLOCKED_RESOURCES')

    def test_old_ready_draft_cannot_be_published_as_compliant(self):
        job = self.service.create_job(task_spec('剪辑', [self.asset_id]))
        self.service.store.set_state(job, 'READY_FOR_REVIEW', message='historical fixture')
        with self.assertRaises(ResourcePolicyBlocked): self.service.publish_job(job)

    def test_legacy_loop_cannot_bypass_policy_or_spend_tokens(self):
        client = Mock()
        output = self.fixture.root / 'legacy'
        with self.assertRaises(ResourcePolicyBlocked):
            run(self.fixture.config, [self.source], 'use external if needed', output, client=client)
        client.complete.assert_not_called(); self.assertFalse(output.exists())

    def test_external_planner_and_packager_stop_before_asset_creation(self):
        from agent_core.packaging import plan_packaging
        provider = Mock()
        with self.assertRaises(ResourcePolicyBlocked): plan_packaging(provider, {}, {}, {})
        provider._json.assert_not_called()
        scripts = Path(__file__).resolve().parents[1] / 'toolchain/scripts'
        with patch('sys.path', [str(scripts), *__import__('sys').path]):
            import native_packaging
            output = self.fixture.root / 'external-layers'
            with patch.object(native_packaging, 'synthesize') as synth:
                with self.assertRaises(ResourcePolicyBlocked):
                    native_packaging.package('missing-install', 'missing-source', 'missing-request', output)
                synth.assert_not_called()
            self.assertFalse(output.exists())
            import native_subtitles
            with self.assertRaises(ResourcePolicyBlocked):
                native_subtitles.add_subtitles('missing', 'missing', 'missing', output, provider='test',
                                               font='C:/Windows/Fonts/msyhbd.ttc')
            self.assertFalse(output.exists())

    def test_cache_presence_or_paid_flags_do_not_unlock_unimplemented_adapter(self):
        cache = self.fixture.root / 'User Data/Cache/ressdk_db/fixture'
        cache.mkdir(parents=True)
        dbpath = cache / 'rp.db'
        with sqlite3.connect(dbpath) as db:
            db.execute('CREATE TABLE effect (is_vip INTEGER, secret TEXT)')
            db.executemany('INSERT INTO effect VALUES (?,?)', [(0, 'private-token'), (1, 'private-token')])
        db.close()
        before = dbpath.read_bytes()
        report = inspect_local_catalog(self.fixture.root / 'User Data')
        self.assertEqual(report['catalog_databases'][0]['tables']['effect'], 2)
        self.assertEqual(report['status'], 'BLOCKED_RESOURCES')
        self.assertNotIn('private-token', json.dumps(report))
        self.assertEqual(before, dbpath.read_bytes())
        self.assertFalse(report['network_requested']); self.assertFalse(report['gui_started'])

    def test_low_level_agent_tools_cannot_bypass_policy(self):
        from agent_core.tools import ToolHub
        with self.assertRaises(ResourcePolicyBlocked):
            ToolHub._build_timeline(None, {})
        from agent_core.commerce import run_ad
        provider = Mock()
        with self.assertRaises(ResourcePolicyBlocked):
            run_ad(None, None, {}, [], None, provider)
        provider.assert_not_called()
