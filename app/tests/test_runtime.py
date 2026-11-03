import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from threading import Event
from agent_core import runtime as r

def response(name=None, args=None, content='done', finish='stop', tokens=5):
    msg={'role':'assistant','content':content}
    if name:
        msg['tool_calls']=[{'id':'call-1','type':'function','function':{'name':name,'arguments':json.dumps(args or {})}}]
    return {'message':msg,'usage':{'total_tokens':tokens},'finish_reason':finish,'request_id':'fixture','model':'fixture-model'}

class FakeClient:
    def __init__(self,responses):self.responses=list(responses);self.received=[]
    def complete(self,messages,tools):
        self.received.append(copy.deepcopy(messages))
        return self.responses.pop(0)

class FakeHub:
    def __init__(self,*args):self.output=args[2]
    def definitions(self):return []
    def execute(self,name,args):return {'ok':name!='failure','tool':name,'value':args}

class RuntimeTests(unittest.TestCase):
    def setUp(self):
        # These isolated fake-model/fake-tool tests exercise the historical loop,
        # not resource readiness. Real entry-point guards have separate tests.
        guard = patch('agent_core.resource_policy.require_internal_resources')
        guard.start(); self.addCleanup(guard.stop)
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.source=self.root/'素材.mp4';self.source.write_bytes(b'media')
        self.config={'provider':{'base_url':'https://example.test/v1','model':'fixture-model','api_key_env':'TEST_AGENT_KEY',
                      'capabilities':{'vision':True,'tool_calls':True}},
                    'toolchain_root':str(self.root),'install_dir':str(self.root),'limits':dict(r.DEFAULTS)}
    def tearDown(self):self.tmp.cleanup()
    def run_fixture(self, responses,hub=FakeHub):
        client=FakeClient(responses)
        result=r.run(self.config,[self.source],'test',self.root/'run',client=client,hub_class=hub)
        return result,client
    def test_real_tool_feedback_is_sent_before_next_decision(self):
        result,client=self.run_fixture([response('inventory'),response()])
        self.assertEqual(result['model_calls'],2);self.assertEqual(result['tool_calls'],1)
        tool=next(m for m in client.received[1] if m['role']=='tool')
        self.assertTrue(json.loads(tool['content'])['ok'])
        self.assertFalse(result['verified_delivery']);self.assertEqual(result['quality_status'],'not_certified')
    def test_stops_at_model_call_limit(self):
        self.config['limits']['max_model_calls']=1
        result,_=self.run_fixture([response('inventory')])
        self.assertEqual(result['status'],'model_call_limit_reached')
    def test_stops_repeated_identical_failures(self):
        result,_=self.run_fixture([response('failure')]*3)
        self.assertEqual(result['status'],'repeated_tool_failure');self.assertEqual(result['tool_calls'],2)
    def test_token_threshold_stops_next_call(self):
        self.config['limits']['token_stop_threshold']=5
        result,client=self.run_fixture([response('inventory',tokens=6)])
        self.assertEqual(result['status'],'token_threshold_reached');self.assertEqual(len(client.received),1)
    def test_incomplete_model_response_never_executes_tools(self):
        result,_=self.run_fixture([response('inventory',finish='length')])
        self.assertEqual(result['status'],'model_response_incomplete');self.assertEqual(result['tool_calls'],0)
    def test_output_existing_and_inside_input_rejected(self):
        output=self.root/'run';output.mkdir()
        with self.assertRaises(FileExistsError):r.run(self.config,[self.source],'',output,client=FakeClient([]),hub_class=FakeHub)
        with self.assertRaises(ValueError):r.run(self.config,[self.root],'',self.root/'nested',client=FakeClient([]),hub_class=FakeHub)
    def test_failure_persists_state_and_releases_lock(self):
        result,_=self.run_fixture([])
        self.assertEqual(result['status'],'failed');self.assertFalse((self.root/'run/.runtime.lock').exists())
        self.assertTrue((self.root/'run/runtime-state.json').is_file())
    def test_inline_key_rejected(self):
        conf={**self.config,'schema':'jianying-external-agent-v1'};conf['provider']['api_key']='must-not-be-stored'
        p=self.root/'config.json';p.write_text(json.dumps(conf))
        with self.assertRaises(ValueError):r.load_config(p)
    def test_doctor_does_not_call_provider(self):
        with patch.dict('os.environ',{'TEST_AGENT_KEY':'sensitive'},clear=False):
            result=r.doctor(self.config)
        self.assertTrue(result['key_present']);self.assertNotIn('sensitive',json.dumps(result))
        self.assertFalse(result['network_requested'])
    def test_png_payload_exact_and_bounded(self):
        p=self.root/'frame.png';raw=b'\x89PNG\r\n\x1a\nfixture';p.write_bytes(raw)
        frame={'path':str(p),'sha256':hashlib.sha256(raw).hexdigest(),'native_pts_us':123}
        msg=r.image_messages([('sample_frames',{'ok':True,'frames':[frame]})],self.root,r.DEFAULTS,True)
        self.assertEqual(msg[0]['content'][1]['type'],'image_url')
        limited={**r.DEFAULTS,'max_images_per_call':1}
        msg=r.image_messages([('sample_frames',{'ok':True,'frames':[frame,frame]})],self.root,limited,True)
        self.assertIn('NOT sent',msg[0]['content'][-1]['text'])
    def test_frames_not_seen_when_vision_disabled(self):
        msg=r.image_messages([('sample_frames',{'ok':True,'frames':[{'path':'never-read.png'}]})],self.root,r.DEFAULTS,False)
        self.assertIn('NOT sent',msg[0]['content'][0]['text'])
    def test_frame_outside_run_or_hash_mismatch_rejected(self):
        p=self.root/'frame.png';p.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
        result=[('preview_draft',{'ok':True,'frames':[{'path':str(p),'sha256':'bad'}]})]
        with self.assertRaises(ValueError):r.image_messages(result,self.root,r.DEFAULTS,True)
        with self.assertRaises(ValueError):r.image_messages(result,self.root/'run',r.DEFAULTS,True)
    def test_doctor_rejects_credential_bearing_endpoint(self):
        self.config['provider']['base_url']='https://user:EXAMPLE_SECRET@example.test/v1?token=EXAMPLE_TOKEN'
        from agent_core.provider import ProviderError
        with self.assertRaises(ProviderError):r.doctor(self.config)
    def test_config_resolves_bundled_toolchain_relative_to_config_file(self):
        conf={**self.config,'schema':'jianying-external-agent-v1','toolchain_root':'toolchain'}
        (self.root/'toolchain').mkdir()
        p=self.root/'config.json';p.write_text(json.dumps(conf),encoding='utf-8')
        loaded=r.load_config(p)
        self.assertEqual(loaded['toolchain_root'],str(self.root/'toolchain'))
    def test_deepseek_profile_uses_standalone_vision_tool_model(self):
        profile=Path(__file__).resolve().parents[1]/'config.example.json'
        loaded=r.load_config(profile)
        self.assertEqual(loaded['provider']['model'],'deepseek-flash')
        self.assertEqual(loaded['provider']['api_key_env'],'DEEPSEEK_API_KEY')
        self.assertTrue(loaded['provider']['capabilities']['vision'])
        self.assertTrue(loaded['provider']['capabilities']['tool_calls'])
        self.assertEqual(loaded['provider']['thinking'],'disabled')
        self.assertNotIn('reasoning_effort',loaded['provider'])
        self.assertTrue((Path(loaded['toolchain_root'])/'scripts/autocut_task.py').is_file())
    def test_entire_history_has_image_budget(self):
        messages=[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:old'}}]*20},
                  {'role':'user','content':[{'type':'image_url','image_url':{'url':'data:new'}}]*20}]
        r.limit_history_images(messages,20)
        self.assertEqual(sum(p['type']=='image_url' for m in messages for p in m['content']),20)
        self.assertTrue(all(p['type']=='text' for p in messages[0]['content']))
    def test_cancel_before_api_call(self):
        stopped=Event();stopped.set();client=FakeClient([])
        state=r.run(self.config,[self.source],'test',self.root/'run',client=client,hub_class=FakeHub,stop_event=stopped)
        self.assertEqual(state['status'],'stopped_by_user');self.assertEqual(client.received,[])

if __name__=='__main__':unittest.main(verbosity=2)
