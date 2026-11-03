import copy
import json
import tempfile
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_core.commerce import compile_ad, rewrite_direct_voiceover, validate_ad_script, NarrationPlanIssue
from agent_core.editing_rules import apply_rules, resolve_rules
from agent_core.model_recovery import model_preflight, ModelRecovery
from agent_core.narration_timing import budget, units
from agent_core.provider import ProviderClient, ProviderError
from tests import test_market_rules, test_task_service

spec = test_market_rules.spec


class CrossMachineFailureTests(unittest.TestCase):
    def test_cached_non_mechanical_voice_request_is_a_listening_goal(self):
        task=spec('日本', '制作日区广告，声音不要机械')
        saved={'schema_version':'1.0','overrides':{},'unsupported':['声音不要机械']}
        before=copy.deepcopy(saved)
        result=apply_rules(task,saved)
        self.assertEqual(saved,before)
        self.assertEqual(result['narration_mode'],'new_native')
        self.assertEqual(result['voice_quality_preferences'],['声音不要机械'])
        self.assertEqual(result['voice_quality_verification'],'final_listening_required')

    def test_quality_preference_does_not_erase_actual_unsupported_request(self):
        task=spec('日本','声音不要机械，克隆指定人物声音')
        with self.assertRaisesRegex(ValueError,'克隆'):
            apply_rules(task,{'schema_version':'1.0','overrides':{},
                              'unsupported':['声音不要机械','克隆指定人物声音']})
        with self.assertRaisesRegex(ValueError,'不能满足'):
            apply_rules(task,{'schema_version':'1.0','overrides':{},
                              'unsupported':['声音不要机械且克隆指定人物声音']})

    def test_new_resolution_retains_quality_goal_without_terminal_failure(self):
        task=spec('美国','声音不要机械')
        model=Mock();model._json.return_value={'schema_version':'1.0','overrides':{},'unsupported':['声音不要机械']}
        self.assertEqual(resolve_rules(model,task)['unsupported'],[])
        self.assertIn('不能写入unsupported',model._json.call_args.args[0])

    def scripts(self):
        fixture,task,plan=test_market_rules.MarketRuleTests().fixture('日本')
        timeline,_,_=compile_ad(task,fixture.assets,fixture.sources,plan)
        maximum,_=budget(task,timeline,len(plan['clips']))
        invalid=copy.deepcopy(plan)
        other=sum(units(task,c['voiceover']) for c in invalid['clips'][1:])
        invalid['clips'][0]['voiceover']='あ'*(maximum-other+5)
        return fixture,task,plan,timeline,invalid,maximum

    def test_five_character_overflow_is_counted_and_repaired_before_return(self):
        f,t,plan,timeline,invalid,maximum=self.scripts()
        model=Mock()
        def response(p):return {'schema_version':'1.0','clips':[
            {'clip_index':i+1,'voiceover':c['voiceover']} for i,c in enumerate(p['clips'])]}
        model._json.side_effect=[response(invalid),response(plan)]
        revised,report=rewrite_direct_voiceover(model,t,f.sources,invalid,timeline,NarrationPlanIssue('整条成品口播总量过长',timeline))
        self.assertEqual(model._json.call_count,2)
        feedback=model._json.call_args.args[1]
        self.assertEqual(feedback['actual_total_speech_units'],maximum+5)
        self.assertEqual(feedback['hard_speech_limit'],maximum)
        validate_ad_script(t,revised,timeline)
        self.assertEqual(revised,plan)

    def test_complete_shorter_alternative_is_selected_without_cutting_words(self):
        f,t,plan,timeline,invalid,maximum=self.scripts()
        response={'schema_version':'1.0','clips':[
            {'clip_index':i+1,'voiceover':c['voiceover'],
             'alternatives':[plan['clips'][i]['voiceover']] if i==0 else []}
            for i,c in enumerate(invalid['clips'])]}
        model=Mock();model._json.return_value=response
        revised,report=rewrite_direct_voiceover(model,t,f.sources,invalid,timeline,NarrationPlanIssue('过长',timeline))
        self.assertEqual(revised,plan)
        self.assertEqual(report['selected_alternative_indices'],[1])
        model._json.assert_called_once()

    def test_repeated_overflow_remains_unapproved_not_silently_truncated(self):
        f,t,plan,timeline,invalid,maximum=self.scripts()
        model=Mock();model._json.return_value={'schema_version':'1.0','clips':[
            {'clip_index':i+1,'voiceover':c['voiceover']} for i,c in enumerate(invalid['clips'])]}
        original=copy.deepcopy(invalid)
        with self.assertRaises(NarrationPlanIssue):
            rewrite_direct_voiceover(model,t,f.sources,invalid,timeline,NarrationPlanIssue('过长',timeline))
        self.assertEqual(invalid,original)
        self.assertEqual(model._json.call_count,4)

    def test_interrupted_preflight_reuses_probe_directory_after_restart(self):
        with tempfile.TemporaryDirectory() as root:
            cfg={'base_url':'https://gateway.test/v1','model':'deepseek-flash','api_key_env':'TEST_KEY'}
            client=ProviderClient(cfg,key_value='private');provider=Mock(client=client,config=cfg)
            directories=[]
            def interrupted(*args,**kwargs):
                directories.append(client.recovery.root)
                raise ProviderError('response pending',category='outcome_uncertain')
            provider._call.side_effect=interrupted
            with self.assertRaises(ProviderError):model_preflight(provider,root)
            provider._call.side_effect=lambda *a,**k:(directories.append(client.recovery.root) or {'message':{'content':'{"ok":true}'}})
            model_preflight(provider,root)
            self.assertEqual(directories[0],directories[1])
            marker=next(Path(root).glob('*.probe.json'))
            self.assertEqual(json.loads(marker.read_text())['state'],'COMPLETED')
            self.assertNotIn('private',marker.read_text())

    def test_network_failures_wait_and_leave_other_jobs_runnable(self):
        f=test_task_service.TaskServiceTests();f.setUp();self.addCleanup(f.tearDown)
        source=f.root/'source.mp4';source.write_bytes(b'isolated')
        aid=f.service.register_asset(source)
        for category in ('timeout','dns','connection','response_incomplete','outcome_uncertain'):
            job=f.service.create_job(spec('日本',assets=[aid]))
            other=f.service.create_job(spec('日本',assets=[aid]))
            provider=Mock(config={'model':'deepseek-flash'},usage={})
            f.service.provider_factory=Mock(return_value=provider)
            with patch('agent_core.task_service.bind_policy'),patch('agent_core.model_recovery.bind_recovery',return_value=provider),\
                 patch('agent_core.model_recovery.model_preflight',side_effect=ProviderError('unavailable',category=category)):
                result=f.service._run_job(job)
            self.assertEqual(result['state'],'WAITING_FOR_INPUT',category)
            self.assertEqual(f.service.get_job(other)['state'],'CREATED')
            self.assertTrue(any(e['type']=='waiting_input' for e in f.service.read_events(job,0)))


class RealTransportRecoveryTests(unittest.TestCase):
    def server(self, *, slow_status=False):
        state={'posts':0,'slow':slow_status,'results':{}}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def send(self,value):
                body=json.dumps(value).encode()
                try:
                    self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
                except OSError:pass  # Expected when the timeout test closes its client socket.
            def do_GET(self):
                if self.path=='/v1/status':
                    if state['slow']:threading.Event().wait(.4)
                    self.send({'recovery_protocol':1});return
                request_id=self.path.split('/')[3]
                self.send(state['results'][request_id] if self.path.endswith('/result') else {'state':'COMPLETED'})
            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                state['posts']+=1;request_id=self.headers['X-Request-Id']
                state['results'][request_id]={'id':'isolated','model':'deepseek-flash','choices':[
                    {'index':0,'message':{'role':'assistant','content':'{"ok":true}'},'finish_reason':'stop'}]}
                # Simulate an accepted request whose response socket disappears.
                self.connection.shutdown(socket.SHUT_RDWR);self.connection.close()
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        cfg={'base_url':f'http://127.0.0.1:{server.server_port}/v1','model':'deepseek-flash',
             'api_key_env':'ISOLATED_KEY','connect_timeout_seconds':.2,'read_timeout_seconds':.1,'total_timeout_seconds':2}
        client=ProviderClient(cfg,key_value='isolated-not-a-real-key');client.gateway=True
        return client,state

    def test_real_dropped_response_recovers_with_one_post(self):
        client,state=self.server()
        with tempfile.TemporaryDirectory() as root:
            recovery=ModelRecovery(root)
            data=recovery.execute(client,b'{"model":"deepseek-flash"}','isolated-not-a-real-key')
            self.assertEqual(json.loads(data)['choices'][0]['message']['content'],'{"ok":true}')
            self.assertEqual(state['posts'],1)
            self.assertEqual(recovery.execute(client,b'{"model":"deepseek-flash"}','isolated-not-a-real-key'),data)
            self.assertEqual(state['posts'],1)

    def test_real_response_header_timeout_is_bounded_and_identified(self):
        client,state=self.server(slow_status=True)
        with self.assertRaises(ProviderError) as caught:
            client._exchange(None,'isolated-not-a-real-key','probe',path='/status')
        self.assertEqual(caught.exception.category,'timeout')
        self.assertEqual(caught.exception.details['phase'],'response_headers')
        self.assertEqual(state['posts'],0)


if __name__=='__main__':unittest.main()
