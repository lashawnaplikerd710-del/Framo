import copy
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_core import native_packaging as p
from agent_core.task_store import digest


def base():
    return {'duration':6000000,'fps':30,'tracks':[{'type':'video','segments':[
        {'id':'first','target_timerange':{'start':0,'duration':3000000},'extra_material_refs':[]},
        {'id':'second','target_timerange':{'start':3000000,'duration':3000000},'extra_material_refs':[]}]}],
        'materials':{'transitions':[]}}


def plan():
    return {'schema':p.SCHEMA,'purpose':'local_test','elements':[
        {'key':'title','kind':'text','start_us':0,'duration_us':6000000,'text':'测试',
         'font_id':'source_han_cn','color':'#FFFFFF','size':15,'x':0,'y':.7}]}


class NativePackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def validate(self,value,snapshot=None):
        with patch.object(p,'resolve_font',return_value={}),patch.object(p,'validate_text'):
            return p.validate_plan(value,snapshot or base(),self.root)

    def test_rejects_unknown_command_fields_out_of_bounds_and_duplicate_keys(self):
        for change in ({'command':'delete'}, {'start_us':6000000}, {'duration_us':1}, {'x':float('nan')},
                       {'color':'red'}, {'font_path':'C:/unverified.ttf'}):
            value=plan();value['elements'][0].update(change)
            with self.assertRaises(p.PackagingFailure):self.validate(value)
        value=plan();value['elements']*=2
        with self.assertRaises(p.PackagingFailure):self.validate(value)

    def test_transition_needs_adjacent_clips_and_cannot_overwrite_existing(self):
        value={'schema':p.SCHEMA,'purpose':'local_test','elements':[
            {'key':'cut','kind':'transition','after_segment_id':'first','resource_id':'123','duration_us':500000}]}
        self.validate(value)
        for mode in ('last','gap','existing','too_long'):
            v=copy.deepcopy(value);s=base()
            if mode=='last':v['elements'][0]['after_segment_id']='second'
            if mode=='gap':s['tracks'][0]['segments'][1]['target_timerange']['start']=4000000
            if mode=='existing':
                s['materials']['transitions']=[{'id':'t'}];s['tracks'][0]['segments'][0]['extra_material_refs']=['t']
            if mode=='too_long':v['elements'][0]['duration_us']=2000000
            with self.assertRaises(p.PackagingFailure):self.validate(v,s)

    def test_commercial_scope_cannot_be_inferred_from_free_status(self):
        client=SimpleNamespace()
        with patch.object(p,'envelope',return_value={'items':[{'resource_id':'123','is_vip':False,'is_business':False}]}):
            self.assertTrue(p.fresh_rights(client,'sticker','123','local_test')['nonmember_metadata_verified'])
            with self.assertRaisesRegex(p.PackagingFailure,'commercial_metadata'):
                p.fresh_rights(client,'sticker','123','commercial')

    def test_hash_verified_payload_cannot_be_replaced_or_escape_receipt(self):
        folder=self.root/'receipt';folder.mkdir();f=folder/'music.m4a';f.write_bytes(b'original')
        proof={'path':str(f),'_directory':str(folder),'files':[{'path':f.name,'bytes':8,'sha256':digest(f)}]}
        p.verify_files(proof)
        f.write_bytes(b'replaced')
        with self.assertRaises(p.PackagingFailure):p.verify_files(proof)
        outside=self.root/'other';outside.write_bytes(b'original');proof['path']=str(outside)
        with self.assertRaises(p.PackagingFailure):p.verify_files(proof)

    def test_uncertain_native_add_restarts_from_checkpoint_without_duplicate(self):
        out=self.root/'out';out.mkdir();source=out/'base';source.mkdir()
        file=source/'draft_content.json';file.write_text('0')
        journal={'base':str(source),'base_sha256':digest(file),'steps':[]}
        client=SimpleNamespace();attempts=[]
        def apply(client,draft,row,proof):
            before=int((draft/'draft_content.json').read_text());attempts.append((draft,before))
            (draft/'draft_content.json').write_text(str(before+1))
            if len(attempts)==1:raise subprocess.TimeoutExpired('native-cli',1)
            return {'key':'title','kind':'text','segment_id':'new-text','native_object_observed':True}
        with patch.object(p,'apply_element',side_effect=apply):
            with self.assertRaises(subprocess.TimeoutExpired):p.run_steps(client,plan(),out,journal)
            self.assertEqual(journal['steps'],[])
            p.run_steps(client,plan(),out,journal)
            p.run_steps(client,plan(),out,journal)
        self.assertEqual([before for _,before in attempts],[0,0])
        self.assertNotEqual(attempts[0][0],attempts[1][0])
        self.assertEqual(len(journal['steps']),1)
        self.assertEqual(file.read_text(),'0')
        self.assertEqual((Path(journal['steps'][0]['draft'])/'draft_content.json').read_text(),'1')

    def test_changed_checkpoint_is_never_reused(self):
        out=self.root/'out';out.mkdir();source=out/'base';source.mkdir()
        f=source/'draft_content.json';f.write_bytes(b'old');sha=digest(f);f.write_bytes(b'changed')
        with self.assertRaisesRegex(p.PackagingFailure,'checkpoint_changed'):
            p.run_steps(SimpleNamespace(),plan(),out,{'base':str(source),'base_sha256':sha,'steps':[]})

    def test_compact_native_normalization_may_add_defaults_but_not_drop_values(self):
        p.assert_retains({'is_minimal_draft':True,'tracks':[{'duration':100}]},
                         {'tracks':[{'duration':100,'volume':1}]})
        for value in ({'tracks':[{'duration':99}]},{'tracks':[{}]},{'tracks':[]}):
            with self.assertRaises(p.PackagingFailure):p.assert_retains({'tracks':[{'duration':100}]},value)

    def test_completed_reuse_checks_draft_media_resources_and_visual_evidence(self):
        out=self.root/'complete';out.mkdir();draft=out/'draft';draft.mkdir()
        native=draft/'draft_content.json';native.write_bytes(b'native-draft')
        media=out/'user.mp4';media.write_bytes(b'user-video')
        frame=out/'frame.png';frame.write_bytes(b'preview')
        resource=out/'resource.m4a';resource.write_bytes(b'official')
        report={'status':'native_packaging_ready_for_review','implementation_sha256':p.IMPLEMENTATION_SHA256,
            'draft':str(draft),'draft_sha256':digest(native),'frames':[{'path':str(frame),'sha256':digest(frame)}],
            'audio_report':None,'delivery_accepted':False}
        (out/'packaging-report.json').write_text(json.dumps(report))
        (out/'journal.json').write_text(json.dumps({'source_media':[{'path':str(media),'sha256':digest(media)}]}))
        proof={'path':str(resource),'_directory':str(out),'files':[{'path':resource.name,'bytes':8,'sha256':digest(resource)}]}
        (out/'resources-verified.json').write_text(json.dumps({'music-123':proof}))
        self.assertEqual(p.validate_completed(out),report)
        for file in (native,media,resource,frame):
            original=file.read_bytes();file.write_bytes(b'changed')
            with self.assertRaises(p.PackagingFailure):p.validate_completed(out)
            file.write_bytes(original)

    def test_completed_reuse_does_not_trust_an_old_implementation(self):
        out=self.root/'complete';out.mkdir()
        (out/'packaging-report.json').write_text(json.dumps({'status':'native_packaging_ready_for_review','implementation_sha256':'old'}))
        (out/'journal.json').write_text('{}')
        with self.assertRaisesRegex(p.PackagingFailure,'implementation_changed'):p.validate_completed(out)


if __name__=='__main__':unittest.main()
