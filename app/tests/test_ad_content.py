import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from agent_core.commerce import (validate_ad_script, planning_evidence, plan_ad, compile_ad,
                                 review_direct_script, rewrite_direct_voiceover)
from agent_core.direct_resources import _cue_text, build_direct_media_plan
from agent_core.timeline_compiler import PlanIssue
from agent_core.task_service import TaskService
from tests import test_commerce as commerce_fixture


class AdContentTests(unittest.TestCase):
    def test_original_transcript_survives_visual_unit_selection(self):
        source={'asset_id':'a','observations':[], 'speech_mode':'visual',
                'speech_units':[{'unit_id':'v001','start_us':0,'end_us':3000000,'text':''}],
                'frames':[{'requested_us':0}],
                'transcript':{'cues':[{'start_us':0,'end_us':3000000,'text':'三面围挡的小窝'}]}}
        row=planning_evidence([source])[0]
        self.assertEqual(row['reference_transcript'][0]['text'],'三面围挡的小窝')
        self.assertEqual(row['speech_units'][0]['text'],'')

    def test_stage_labels_never_become_voice_even_with_extra_words(self):
        task={'narration_mode':'new_native'}
        for text in ('开场亮相','现在开始细节推进吧','三面围挡的小窝'):
            plan={'clips':[{'heading':'三面围挡的小窝','voiceover':text}]}
            with self.assertRaises(PlanIssue): validate_ad_script(task,plan)

    def test_headings_cannot_replace_missing_or_partial_narration(self):
        timeline={'duration_us':3000000,'captions':[{'start_us':0,'end_us':3000000,'text':'开场亮相'}]}
        with self.assertRaises(ValueError): _cue_text({},timeline,required=True)
        timeline['narration_cues']=[{'start_us':0,'end_us':1000000,'text':'这是宠物的小窝。'},
                                   {'start_us':1000000,'end_us':2000000,'text':''}]
        with self.assertRaises(ValueError): _cue_text({},timeline,required=True)

    def test_explicit_voice_track_is_used_instead_of_caption_title(self):
        timeline={'duration_us':3000000,'captions':[{'text':'开场亮相'}],
                  'narration_cues':[{'start_us':0,'end_us':3000000,'text':'给毛孩子一个小窝。'}]}
        self.assertEqual(_cue_text({},timeline,required=True)[0]['text'],'给毛孩子一个小窝。')

    def test_direct_compiler_requires_real_script_and_preserves_legacy(self):
        fixture=commerce_fixture.CommerceTests(); fixture.setUp()
        compile_ad(fixture.task, fixture.assets, fixture.sources, fixture.plan)
        task={**fixture.task,'render_backend':'direct_ffmpeg'}
        with self.assertRaises(PlanIssue): compile_ad(task,fixture.assets,fixture.sources,fixture.plan)
        plan=copy.deepcopy(fixture.plan)
        for i,c in enumerate(plan['clips']): c['voiceover']=f'给爱宠安排第{i+1}款小窝。'
        timeline,_,_=compile_ad(task,fixture.assets,fixture.sources,plan)
        self.assertEqual(len(timeline['narration_cues']),4)
        self.assertEqual(timeline['captions'][0]['text'],plan['clips'][0]['voiceover'])

    def test_plan_gets_actual_images_but_crossborder_never_requests_voice(self):
        provider=Mock(); source={'asset_id':'a','observations':[], 'speech_units':[],
                               'frames':[{'path':'frame.png','requested_us':0}]}
        plan_ad(provider,{'workflow':'crossborder_montage','narration_mode':'text_only'},[source])
        self.assertEqual(provider._json.call_args.kwargs['images'],source['frames'])
        self.assertNotIn('"voiceover":',provider._json.call_args.args[0])

    def test_required_resources_are_reported_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            task={'workflow':'product_ad','narration_mode':'text_only',
                  'formal_ad_policy':{'music_required':True,'sound_effects_required':True}}
            result=build_direct_media_plan(task,{'duration_us':3000000,'source_map':[]},{},Path(tmp),{})
            self.assertEqual({x['kind'] for x in result['pending']},{'music','sound'})

    def test_review_rejects_accept_with_unresolved_issues(self):
        provider=Mock()
        provider._json.return_value={'schema_version':'1.0','decision':'accept',
            'issues':['商品不一致'],'product':'宠物窝','limits':'抽样'}
        with self.assertRaises(PlanIssue):
            review_direct_script(provider,{},[],{}, {'source_map':[],'narration_cues':[]})

    def test_review_records_image_rejection_without_claiming_visual_acceptance(self):
        provider=Mock()
        provider._json.return_value={'schema_version':'1.0','decision':'accept',
            'issues':[],'product':'宠物窝','limits':'仅核验文字',
            '_input_evidence':{'images':'rejected_by_provider'}}
        sources=[{'asset_id':'a','frames':[{'path':'a.jpg','requested_us':1}],
                  'speech_units':[],'observations':[]}]
        timeline={'source_map':[{'asset_id':'a','source_start_us':0,'source_end_us':2}],
                  'narration_cues':[]}
        result=review_direct_script(provider,{},sources,{},timeline)
        self.assertFalse(result['sampled_visual_review'])
        self.assertFalse(result['audio_listened'])

    def test_voice_repair_cannot_change_shot_positions(self):
        fixture=commerce_fixture.CommerceTests();fixture.setUp()
        timeline,_,_=compile_ad(fixture.task,fixture.assets,fixture.sources,fixture.plan)
        provider=Mock()
        provider._json.return_value={'schema_version':'1.0','clips':[
            {'clip_index':i+1,'voiceover':'给爱宠一个柔软小窝。','heading':'柔软小窝',
             'asset_id':'invented','first_unit':'v999'} for i in range(4)]}
        revised,report=rewrite_direct_voiceover(provider,fixture.task,fixture.sources,
                                                fixture.plan,timeline,'太长')
        for old,new in zip(fixture.plan['clips'],revised['clips']):
            self.assertEqual({k:v for k,v in old.items() if k not in ('heading','voiceover')},
                             {k:v for k,v in new.items() if k not in ('heading','voiceover')})
        self.assertTrue(report['shot_positions_unchanged'])

    def test_content_feedback_gets_local_repair_even_on_last_planning_round(self):
        fixture=commerce_fixture.CommerceTests();fixture.setUp()
        task={**fixture.task,'render_backend':'direct_ffmpeg','max_revision_rounds':0}
        plan=copy.deepcopy(fixture.plan)
        for i,c in enumerate(plan['clips']):c['voiceover']=f'给爱宠安排第{i+1}款小窝。'
        provider=Mock()
        provider._json.side_effect=[plan,
            {'schema_version':'1.0','decision':'revise','issues':['精简卖点'], 'product':'宠物窝','limits':'文字'},
            {'schema_version':'1.0','clips':[{'clip_index':i+1,'heading':c['heading'],
              'voiceover':f'给毛孩子选第{i+1}款小窝。'} for i,c in enumerate(plan['clips'])]},
            {'schema_version':'1.0','decision':'accept','issues':[], 'product':'宠物窝','limits':'文字'}]
        with tempfile.TemporaryDirectory() as tmp:
            service=TaskService.__new__(TaskService);service.store=Mock()
            service.store.job_dir.return_value=Path(tmp)
            service.store.list_artifacts.return_value=[]
            result,timeline,_,_=service._plan_direct_ad('job_test',task,fixture.assets,fixture.sources,provider)
            self.assertEqual(timeline['ad_content_review']['decision'],'accept')
            self.assertEqual(result['clips'][0]['first_unit'],plan['clips'][0]['first_unit'])
            self.assertIn('给毛孩子',result['clips'][0]['voiceover'])
            self.assertEqual(provider._json.call_count,4)

    def test_too_fast_script_and_unsupported_claim_fail_before_tts(self):
        timeline={'source_map':[{'timeline_start_us':0,'timeline_end_us':1000000}]}
        for text in ('宠物窝只要十来块。','三面围挡的小窝搭配软垫让爱宠躺下来。'):
            with self.assertRaises(PlanIssue):
                validate_ad_script({'narration_mode':'new_native'},
                    {'clips':[{'heading':'宠物窝','voiceover':text}]}, timeline)


if __name__=='__main__': unittest.main()
