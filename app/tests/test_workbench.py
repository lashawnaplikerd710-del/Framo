import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, Mock
from http.server import ThreadingHTTPServer

from agent_core.workbench import Workbench, Handler

TOKEN='jyg_'+'a'*43
SETTINGS={'gateway_url':'','install_dir':'','draft_root':'C:/drafts','user_data_dir':'C:/userdata'}


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        with patch('agent_core.workbench.default_settings',return_value=dict(SETTINGS)):
            self.app=Workbench(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()

    def test_settings_persist_paths_but_never_token(self):
        with patch('agent_core.workbench.save_token') as save, patch.object(self.app,'_configure'):
            self.app.save_settings({'gateway_url':'https://work.example/v1','access_token':TOKEN})
            save.assert_called_once()
        stored=self.app.settings_path.read_text(encoding='utf-8')
        self.assertNotIn(TOKEN,stored)
        self.assertNotIn('access_token',stored)
        self.assertEqual(json.loads(stored)['gateway_url'],'https://work.example/v1')

    def test_settings_frozen_during_live_work(self):
        with patch.object(self.app,'active',return_value=True),patch('agent_core.workbench.save_token') as save:
            with self.assertRaises(ValueError):self.app.save_settings({'gateway_url':'https://work.example/v1'})
            save.assert_not_called()

    def test_incomplete_environment_prevents_job(self):
        with patch.object(self.app,'checks',return_value=[{'ok':False}]):
            with self.assertRaises(ValueError):self.app.create_job({'instruction':'剪辑','asset_ids':[]})

    def test_output_directory_persists_and_applies_only_to_new_specs(self):
        first = Path(self.tmp.name)/'成品甲'
        second = Path(self.tmp.name)/'成品乙'
        self.app.config_path.write_text('{"old_runtime":true}', encoding='utf-8')
        with patch.object(self.app, '_configure') as configure, patch.object(self.app,'active',return_value=True):
            saved = self.app.save_output_directory(str(first))
            configure.assert_not_called()
        self.assertEqual(saved['output_dir'], str(first.resolve()))
        reopened = Workbench(self.tmp.name)
        self.assertEqual(reopened.default_output_directory(), str(first.resolve()))
        original = {'render_backend':'direct_ffmpeg'}
        old_spec = self.app._bind_output_directory(original)
        self.app.save_output_directory(str(second))
        self.assertEqual(old_spec['output_dir'], str(first.resolve()))
        self.assertNotIn('output_dir', original)
        self.assertEqual(self.app._bind_output_directory(original)['output_dir'], str(second.resolve()))
        self.assertEqual(self.app.config_path.read_text(encoding='utf-8'), '{"old_runtime":true}')
        self.assertNotIn('output_dir', self.app._bind_output_directory({'render_backend':'native_jianying'}))

    def test_output_directory_rejects_source_draft_task_and_unwritable_folders(self):
        from agent_core.task_store import TaskStore
        store = TaskStore(Path(self.tmp.name)/'tasks')
        source = Path(self.tmp.name)/'sources'/'original.mp4';source.parent.mkdir();source.write_bytes(b'original')
        self.app.service = Mock(store=store, config={})
        with store._connect() as db:
            # Use the normal asset registration model for path protection.
            columns = [r[1] for r in db.execute('PRAGMA table_info(assets)')]
        with patch.object(store, '_connect') as connection:
            connection.return_value.__enter__.return_value.execute.return_value = [
                (json.dumps({'path':str(source)}),)]
            for folder in [source.parent, Path(self.tmp.name)/'tasks'/'output', Path(SETTINGS['draft_root'])/'output']:
                with self.assertRaises(ValueError): self.app.save_output_directory(str(folder))
        self.app.service = None
        with patch('tempfile.TemporaryFile', side_effect=PermissionError('read only')):
            with self.assertRaises(PermissionError):self.app.save_output_directory(str(Path(self.tmp.name)/'denied'))
        self.assertNotIn('direct_output_dir', self.app.settings)
        self.assertFalse(self.app.settings_path.exists())
        self.assertEqual(source.read_bytes(), b'original')

    def test_single_and_batch_capture_selected_default_directory(self):
        folder = str(Path(self.tmp.name)/'outputs')
        self.app.settings['direct_output_dir'] = folder
        self.app.service = Mock()
        with patch.object(self.app, 'checks', return_value=[{'ok':True}]):
            self.app.create_job({'instruction':'剪一条视频','asset_ids':['asset_a']})
            self.assertEqual(self.app.service.create_job.call_args.args[0]['output_dir'], folder)
            self.app.create_batch({'instruction':'剪一条视频','asset_ids':['asset_a'],'output_count':3})
            self.assertEqual([s['output_dir'] for s in self.app.service.create_batch.call_args.args[0]], [folder]*3)

    def test_create_uses_service_and_keeps_editing_out_of_http_layer(self):
        self.app.service=Mock();self.app.service.create_job.return_value='job_'+'a'*32
        with patch.object(self.app,'checks',return_value=[{'ok':True}]):
            result=self.app.create_job({'instruction':'28 秒全新口播混剪草稿','asset_ids':['asset_a','asset_b']})
        self.assertTrue(result['queued'])
        spec=self.app.service.create_job.call_args.args[0]
        self.assertEqual(spec['resource_use'],'local_review')
        self.assertEqual(spec['workflow'],'custom')
        self.assertEqual(spec['target']['duration_ms'],28000)

    def test_job_status_handles_unspecified_target_duration(self):
        self.app.service=Mock()
        self.app.service.get_job.return_value={
            'job_id':'job_'+'b'*32, 'state':'CREATED', 'task_spec':{
                'instruction':'单素材剪辑', 'workflow':'talking_head',
                'target':{'duration_ms':None}, 'asset_ids':[]},
            'error':None, 'cancel_requested':False, 'created_at':'now'}
        self.app.service.list_artifacts_safe.return_value=([], [])
        self.app.service.read_events.return_value=[]
        self.app.service.store.batch_for_job.return_value=None
        self.app.service.store.job_dir.return_value=Path(self.tmp.name)
        result=self.app.job('job_'+'b'*32)
        self.assertEqual(result['timing']['features']['duration_s'], 0.0)

    def test_gateway_grant_is_redacted_even_when_not_in_environment(self):
        self.assertNotIn(TOKEN,self.app.safe_error(ValueError('invalid '+TOKEN)))

    def test_partial_native_checkpoint_is_visible_without_touching_worker(self):
        self.app.service=Mock();self.app.service.store.job_dir.return_value=Path(self.tmp.name)
        native=Path(self.tmp.name)/'n001';native.mkdir()
        (native/'journal.json').write_text(json.dumps({'steps':[{'key':'bgm'}]}))
        (native/'plan.json').write_text(json.dumps({'elements':[{},{}]}))
        self.assertEqual(self.app.native_progress('job_x')['completed'],1)
        self.assertEqual(self.app.native_progress('job_x')['total'],2)
        (native/'journal.json').write_text('{')
        self.assertIsNone(self.app.native_progress('job_x'))

    def test_stale_background_worker_is_recoverable(self):
        job_dir = Path(self.tmp.name) / 'job'
        job_dir.mkdir()
        started = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        report = job_dir / 'worker.json'
        report.write_text(json.dumps({'status': 'running', 'started_at': started}), encoding='utf-8')
        (job_dir / 'background-latest.json').write_text(json.dumps({
            'pid': 2_000_000_000, 'started_at': started, 'status': 'background_launch_accepted',
            'worker_report': str(report)}), encoding='utf-8')
        self.app.service = Mock()
        self.app.service.store.job_dir.return_value = job_dir
        job = {'job_id': 'job_' + 'a' * 32, 'state': 'ANALYZING', 'worker_running': False}
        self.assertTrue(self.app._stale_worker(job))


class HttpBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.server.host_header='127.0.0.1:'+str(cls.server.server_port)
        cls.server.session='session-test';cls.server.csrf='csrf-test'
        cls.server.app=Mock()
        cls.server.app.status.return_value={'settings':{'gateway_url':'https://work.example/v1'}}
        cls.server.app.safe_error.side_effect=lambda e:str(e)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()

    def request(self,method,path,body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        conn.request(method,path,body,headers or {})
        response=conn.getresponse();raw=response.read();result=(response.status,dict(response.getheaders()),raw)
        conn.close();return result

    def test_unauthorized_cannot_read_settings_or_write_files(self):
        for method,path in [('GET','/api/status'),('POST','/api/upload'),('POST','/api/jobs')]:
            self.assertEqual(self.request(method,path)[0],403)

    def test_browser_origin_and_host_must_match(self):
        data=json.dumps({'token':self.server.session})
        headers={'Content-Type':'application/json','Origin':'https://evil.example'}
        self.assertEqual(self.request('POST','/api/unlock',data,headers)[0],403)
        self.assertEqual(self.request('GET','/',headers={'Host':'evil.example'})[0],403)

    def test_unlock_and_csrf_boundary(self):
        headers={'Content-Type':'application/json','Origin':'http://'+self.server.host_header}
        code,reply,raw=self.request('POST','/api/unlock',json.dumps({'token':'session-test'}),headers)
        self.assertEqual(code,200);self.assertIn('HttpOnly',reply['Set-Cookie']);self.assertIn('SameSite=Strict',reply['Set-Cookie'])
        cookie=reply['Set-Cookie'].split(';')[0]
        self.assertEqual(self.request('GET','/api/status',headers={'Cookie':cookie})[0],200)
        self.assertEqual(self.request('POST','/api/settings','{}',{'Cookie':cookie,'Content-Type':'application/json'})[0],403)

    def test_static_paths_and_security_headers(self):
        code,headers,raw=self.request('GET','/')
        self.assertEqual(code,200);self.assertIn("frame-ancestors 'none'",headers['Content-Security-Policy'])
        self.assertIn('新建剪辑',raw.decode())
        self.assertEqual(self.request('GET','/../config.native.json')[0],403)


if __name__=='__main__':unittest.main()
