import copy
import json
import tempfile
import unittest
from unittest.mock import Mock
from pathlib import Path

from agent_core import original_montage as om
from agent_core.workbench import Workbench
from agent_core.editing_rules import apply_rules
from agent_core.visual_presets import visual_plan
from agent_core.commerce import editing_units,compile_ad,plan_ad
from agent_core.timeline_compiler import PlanIssue
from tests.test_long_video import fixture


class OriginalMontageTests(unittest.TestCase):
    def task(self):
        return Workbench._prepare_spec(None,{'workflow':om.WORKFLOW,'instruction':'做3分钟混剪，保留原声',
            'asset_ids':['a','b'],'target_duration_ms':180000})

    def test_profile_preserves_source_after_model_override(self):
        task=self.task()
        task['instruction']='添加配音和字幕'
        resolved={'schema_version':'1.0','overrides':{'narration':{'value':'new','quote':'添加配音'},
            'text':{'value':True,'quote':'字幕'}},'unsupported':[]}
        task=apply_rules(task,resolved)
        self.assertTrue(om.active(task));self.assertEqual(task['narration_mode'],'preserve_source')
        self.assertFalse(task['constraints']['subtitles']);self.assertFalse(task['mute_source_audio'])
        self.assertFalse(task['separate_narration']);self.assertNotIn('text',task['enabled_direct_resources'])

    def test_timed_tokens_end_before_next_sentence(self):
        asset={'video_duration_us':9000000,'audio_duration_us':9000000}
        transcript={'timed_tokens':{'tokens':['这是','完整','句子','。','下一','句话','。'],
            'timestamps':[.1,.8,1.4,2.1,2.4,3.1,4.0]}}
        rows=om.semantic_units(asset,transcript)
        self.assertEqual(len(rows),2)
        self.assertLess(rows[0]['end_us'],2400000)
        self.assertLessEqual(rows[0]['end_us'],rows[1]['start_us'])
        self.assertEqual(rows[1]['text'],'下一句话。')

    def test_duration_fitter_never_repeats_or_trims_a_unit(self):
        pool=[{'key':str(i),'frames':n} for i,n in enumerate([80,70,50,60,90])]
        selected=om._fit(pool,180,0)
        self.assertEqual(sum(s['frames'] for s in selected),180)
        self.assertEqual(len({s['key'] for s in selected}),len(selected))
        with self.assertRaises(PlanIssue):om._fit(pool,1000,0)

    def expansion_fixture(self):
        catalog={f'{i%2}:s{i:04d}':{'key':f'{i%2}:s{i:04d}',
            'asset_id':str(i%2),'unit_index':i//2,'unit_id':f's{i:04d}',
            'start_us':i//2*3_000_000,'end_us':(i//2+1)*3_000_000,
            'text':'同主题完整表达。','frames':90} for i in range(12)}
        keys=list(catalog)
        response={'chapters':[{'title':'清洁演示','unit_ids':keys[:2]}]}
        return catalog,keys,response

    def test_short_pool_gets_semantic_additions_before_whole_unit_fitting(self):
        catalog,keys,response=self.expansion_fixture();provider=Mock()
        provider._json.return_value={'additions':[{'chapter_index':0,'unit_ids':keys[2:8]}]}
        updated,pool,groups,repairs=om._complete_candidates(provider,response,catalog,30,540,0)
        selected=om._fit(pool,540,0,anchors=True)
        self.assertEqual(sum(r['frames'] for r in selected),540)
        self.assertEqual(len({k for r in selected for k in r['members']}),6)
        self.assertEqual(len(response['chapters'][0]['unit_ids']),2) # Never mutate a prior proposal.
        self.assertEqual(len(repairs),1);provider._json.assert_called_once()
        payload=provider._json.call_args.args[1]
        self.assertEqual(payload['minimum_additional_seconds'],12)
        self.assertTrue(all(r['id'] not in keys[:2] for r in payload['unused_units']))

    def test_expansion_never_invents_unknown_ids_or_discards_protected_groups(self):
        catalog,keys,response=self.expansion_fixture();provider=Mock()
        provider._json.return_value={'additions':[{'chapter_index':0,'unit_ids':['invented']}]}
        with self.assertRaisesRegex(PlanIssue,'不存在的语义单元') as caught:
            om._complete_candidates(provider,response,catalog,30,540,0)
        self.assertIn('invented',caught.exception.rejected_original_plan['chapters'][0]['unit_ids'])
        provider._json.assert_called_once()

    def test_expansion_is_bounded_and_preserves_latest_proposal_for_feedback(self):
        catalog,keys,response=self.expansion_fixture();provider=Mock()
        provider._json.return_value={'additions':[]}
        with self.assertRaisesRegex(PlanIssue,'已自动补选两轮') as caught:
            om._complete_candidates(provider,response,catalog,30,540,0)
        self.assertEqual(provider._json.call_count,2)
        self.assertEqual(caught.exception.rejected_original_plan,response)

    def test_genuine_source_shortage_does_not_call_model_to_fill_clock(self):
        catalog,keys,response=self.expansion_fixture();provider=Mock()
        with self.assertRaisesRegex(PlanIssue,'实际可用完整原声语句不足'):
            om._complete_candidates(provider,response,catalog,30,10_000,0)
        provider._json.assert_not_called()

    def test_frame_count_matches_compiler_instead_of_truncating_each_clip(self):
        self.assertEqual(om.frame_count(33333,3366666,30),100)
        self.assertEqual(om.frame_count(166666,3666666,30),105)

    def test_long_default_keeps_old_three_transitions_new_mode_uses_twelve(self):
        normal=visual_plan({'workflow':'custom','instruction':''},'stable')
        task=self.task();task['instruction']=''
        new=visual_plan(task,'stable')
        self.assertEqual(len(normal['categories']['transition']['presets']),3)
        self.assertEqual(len(new['categories']['transition']['presets']),12)

    def test_real_compiler_accepts_more_than_forty_original_units(self):
        task,assets,sources,_=fixture(duration_ms=600000,count=1)
        task['original_montage_policy']=om.POLICY
        task['constraints']['allow_reorder']=True
        task=om.enforce(task);task['target']['duration_ms']=None
        units=[{'unit_id':f's{i:04d}','start_us':i*2000000,'end_us':(i+1)*2000000,'text':'完整口播。'} for i in range(50)]
        sources[0].update(speech_mode='speech',speech_units=units,
                          frames=[{'requested_us':i*2000000} for i in range(50)])
        plan={'schema_version':'1.0','title':'语义混剪','story':'按话题交叉',
            'clips':[{'asset_id':assets[0]['asset_id'],'first_unit':u['unit_id'],'last_unit':u['unit_id'],
                      'frame_times_us':[u['start_us']],'heading':'','palette':'cream','placement':'lower',
                      'role':'chapter','reason':'完整表达'} for u in reversed(units)]}
        timeline,_,_=compile_ad(task,assets,sources,plan)
        self.assertEqual(len(timeline['source_map']),50)
        self.assertEqual(timeline['duration_us'],100000000)
        self.assertFalse(timeline['captions'])

    def test_planner_uses_real_ids_and_reports_actual_switches(self):
        task=self.task();task['target'].update(duration_ms=12000,tolerance_ms=0)
        task.pop('opening_policy',None)  # Saved legacy jobs retain their original semantics.
        sources=[]
        for aid in ['a','b']:
            sources.append({'asset_id':aid,'speech_units':[{'unit_id':f's{i}', 'start_us':i*3000000,
                'end_us':(i+1)*3000000,'text':f'{aid}{i}完整句子。'} for i in range(4)],
                'frames':[{'requested_us':i*1000000} for i in range(12)],'observations':[]})
        provider=Mock();provider._json.return_value={'title':'新开头','chapters':[
            {'title':'演示','unit_ids':['1:s2','0:s2','1:s0','0:s0','1:s1','0:s1']} ]}
        plan=om.plan(provider,task,sources)
        self.assertEqual(plan['original_montage_report']['selected_duration_s'],12)
        self.assertGreaterEqual(plan['original_montage_report']['source_switches'],2)
        self.assertTrue(all(c['heading']=='' for c in plan['clips']))

    def test_unknown_unit_is_rejected_instead_of_invented(self):
        provider=Mock();provider._json.return_value={'chapters':[{'unit_ids':['invented']} ]}
        with self.assertRaises(PlanIssue):om.plan(provider,self.task(),[])

    def test_protected_original_speech_gap_restores_intervening_units(self):
        catalog={f'1:s{i}':{'key':f'1:s{i}','asset_id':'b','unit_index':i,
            'start_us':i*3_000_000,'end_us':(i+1)*3_000_000} for i in range(4)}
        self.assertEqual(om._protected_members(['1:s0','1:s2','1:s3'],catalog),list(catalog))
        self.assertEqual(om._protected_members(['1:s3','1:s2'],catalog),['1:s2','1:s3'])
        with self.assertRaises(PlanIssue):om._protected_members(['1:s2','1:s2'],catalog)

    def test_cross_source_protected_group_remains_rejected_with_exact_feedback(self):
        task=self.task();provider=Mock()
        task.pop('opening_policy',None)
        sources=[{'asset_id':aid,'speech_units':[{'unit_id':'s1','start_us':0,'end_us':3_000_000,'text':'完整表达。'}],
            'frames':[{'requested_us':0}],'observations':[]} for aid in ('a','b')]
        response={'chapters':[{'unit_ids':[['0:s1','1:s1']]}]};provider._json.return_value=response
        with self.assertRaises(PlanIssue) as caught:om.plan(provider,task,sources)
        self.assertIn('0:s1',str(caught.exception));self.assertIn('1:s1',str(caught.exception))
        self.assertEqual(caught.exception.rejected_original_plan,response)

    def test_distant_group_cannot_expand_beyond_sixty_seconds(self):
        catalog={f'0:s{i}':{'key':f'0:s{i}','asset_id':'a','unit_index':i,
            'start_us':i*3_000_000,'end_us':(i+1)*3_000_000} for i in range(25)}
        with self.assertRaisesRegex(PlanIssue,'60秒'):om._protected_members(['0:s0','0:s24'],catalog)

    def test_explicit_sparse_audio_overrides_dense_profile(self):
        from agent_core.sound_design import arrange
        task=self.task();task['instruction']='少量音效，保持舒缓'
        timeline={'duration_us':40000000,'source_map':[{'timeline_start_us':i*4000000,
            'timeline_end_us':(i+1)*4000000} for i in range(10)]}
        plan={'clips':[{'role':'detail','reason':'展示'} for _ in range(10)]}
        candidates=[{'sha256':str(i)*64,'duration_us':100000,'family':'accent','path':'unused.ogg'} for i in range(1,4)]
        result=arrange(task,timeline,plan,candidates=candidates)
        self.assertEqual(result['density'],'sparse');self.assertEqual(result['min_gap_us'],4000000)


if __name__=='__main__':unittest.main()
