import copy
import unittest
from agent_core.commerce import (compile_ad, speech_units, review_ad, planning_evidence,
                                  plan_feedback, editing_units, repair_direct_ad_duration)
from agent_core.task_service import task_spec
from agent_core.timeline_compiler import PlanIssue


class CommerceTests(unittest.TestCase):
    def test_new_narration_uses_visual_windows_even_when_lyrics_have_punctuation(self):
        asset={'video_duration_us':12_000_000,'audio_duration_us':12_000_000}
        transcript={'cues':[{'start_us':500_000,'end_us':3_000_000,'text':'每分每秒陪伴你的苦乐。'}]}
        frames=[{'requested_us':i*1_000_000} for i in range(12)]
        units,mode=editing_units({'target':{'fps':30},'narration_mode':'new_native'},asset,transcript,frames)
        self.assertEqual(mode,'visual');self.assertGreater(len(units),1)
        self.assertEqual(units[-1]['end_us'],12_000_000)
        speech,mode=editing_units({'target':{'fps':30},'narration_mode':'preserve_source'},asset,transcript,frames)
        self.assertEqual(mode,'speech');self.assertEqual(len(speech),1)

    def test_copy_only_muted_audio_does_not_limit_visual_selection(self):
        asset = {'video_duration_us': 9_000_000, 'audio_duration_us': 9_000_000}
        transcript = {'cues': [{'start_us': 0, 'end_us': 2_000_000, 'text': 'reference.'}]}
        task = {'target': {'fps': 30}, 'narration_mode': 'text_only', 'mute_source_audio': True}
        units, mode = editing_units(task, asset, transcript, [{'requested_us': i*1_000_000} for i in range(9)])
        self.assertEqual(mode, 'visual')
        self.assertEqual(units[-1]['end_us'], 9_000_000)

    def test_custom_unrequested_short_duration_preserves_explicit_duration_checks(self):
        task = copy.deepcopy(self.task); task['workflow'] = 'custom'
        sources = copy.deepcopy(self.sources)
        for source in sources:
            for unit in source['speech_units']:
                unit['start_us'] //= 2; unit['end_us'] //= 2
            source['frames'] = [{'requested_us': 1_000_000}, {'requested_us': 3_000_000}]
        plan = copy.deepcopy(self.plan)
        for clip in plan['clips']:
            clip['frame_times_us'] = [t//2 for t in clip['frame_times_us']]
        timeline, _, _ = compile_ad(task, self.assets, sources, plan)
        self.assertEqual(timeline['duration_us'], 8_000_000)
        task['target']['duration_ms'] = 28_000
        with self.assertRaises(PlanIssue):
            compile_ad(task, self.assets, sources, plan)

    def test_duration_feedback_offers_only_measured_nonoverlap_repairs(self):
        task=copy.deepcopy(self.task);task['target'].update(duration_ms=20_000,tolerance_ms=1000)
        sources=copy.deepcopy(self.sources)
        sources[0]['speech_units'].append({'unit_id':'u003','start_us':8_000_000,'end_us':12_000_000,'text':'收束。'})
        feedback=plan_feedback(task,sources,self.plan,PlanIssue('short'))
        options=feedback['duration_repair_options'];self.assertTrue(options)
        self.assertTrue(all(o['resulting_total_ms']==20_000 for o in options))
        self.assertTrue(all(o['last_unit']=='u003' for o in options))

    def test_duration_feedback_offers_split_for_long_visual_clip(self):
        task=copy.deepcopy(self.task);task['target'].update(duration_ms=12_000,tolerance_ms=1000)
        source=copy.deepcopy(self.sources[0]); source['speech_mode']='visual'
        source['speech_units']=[
            {'unit_id':f'v{i+1:03d}','start_us':i*4_000_000,
             'end_us':(i+1)*4_000_000,'text':'','visual_only':True} for i in range(3)]
        source['frames']=[{'requested_us':i*2_000_000} for i in range(7)]
        proposal={'clips':[{'asset_id':source['asset_id'],'first_unit':'v001','last_unit':'v003'}]}
        feedback=plan_feedback(task,[source],proposal,PlanIssue('too long'))
        self.assertEqual(feedback['visual_split_options'][0]['operation'],'split_clip')
        spans=feedback['visual_split_options'][0]['replacement_spans']
        self.assertEqual(sum(s['duration_ms'] for s in spans), 12000)
        self.assertTrue(all(1000 <= s['duration_ms'] <= 10000 for s in spans))

    def test_local_visual_duration_repair_converges_when_model_story_total_is_wrong(self):
        task=copy.deepcopy(self.task)
        task['target'].update(duration_ms=28_000, tolerance_ms=3_000)
        task['narration_mode']='new_native'
        durations=[31_067_000,23_234_000]
        sources=[]
        for source, duration in zip(self.sources, durations):
            frames=[{'requested_us': (duration - 1) * i // 15} for i in range(16)]
            units, mode=editing_units(task, {'video_duration_us': duration}, {}, frames)
            sources.append({**copy.deepcopy(source), 'speech_units': units,
                            'speech_mode': mode, 'frames': frames,
                            'video_duration_us': duration,
                            'audio_duration_us': duration - (46_000 if duration == durations[0] else 61_000),
                            'has_audio': True})
        proposal={
            'schema_version':'1.0', 'title':'视觉时长收敛', 'story':'模型叙述总时长与实测不同',
            'clips': [
                {'asset_id':sources[0]['asset_id'],'first_unit':'v001','last_unit':'v001',
                 'role':'hook','reason':'开场主体','frame_times_us':[0],
                 'heading':'主体亮相','palette':'cream','placement':'upper'},
                {'asset_id':sources[1]['asset_id'],'first_unit':'v001','last_unit':'v003',
                 'role':'proof','reason':'展示细节','frame_times_us':[0],
                 'heading':'细节呈现','palette':'mint','placement':'lower'},
                {'asset_id':sources[0]['asset_id'],'first_unit':'v002','last_unit':'v003',
                 'role':'use','reason':'展示形态','frame_times_us':[0],
                 'heading':'整体一览','palette':'pink','placement':'upper'},
                {'asset_id':sources[1]['asset_id'],'first_unit':'v004','last_unit':'v006',
                 'role':'use','reason':'展示操作','frame_times_us':[0],
                 'heading':'操作展示','palette':'cream','placement':'lower'},
                {'asset_id':sources[0]['asset_id'],'first_unit':'v004','last_unit':'v005',
                 'role':'proof','reason':'补充角度','frame_times_us':[0],
                 'heading':'角度补充','palette':'mint','placement':'upper'},
                {'asset_id':sources[1]['asset_id'],'first_unit':'v008','last_unit':'v008',
                 'role':'closing','reason':'自然收束','frame_times_us':[0],
                 'heading':'收束定格','palette':'pink','placement':'lower'},
            ]}
        repaired=repair_direct_ad_duration(task, sources, proposal)
        self.assertIsNotNone(repaired)
        plan, report=repaired
        self.assertTrue(25_000 <= report['measured_total_ms'] <= 31_000)
        self.assertGreaterEqual(len(plan['clips']), 4)
        self.assertGreaterEqual(len({clip['asset_id'] for clip in plan['clips']}), 2)
        self.assertEqual(plan['clips'][-1]['role'], 'closing')
        for source in sources:
            selected=[clip for clip in plan['clips'] if clip['asset_id']==source['asset_id']]
            units={unit['unit_id']:unit for unit in source['speech_units']}
            masks=[]
            for clip in selected:
                first=next(i for i,u in enumerate(source['speech_units']) if u['unit_id']==clip['first_unit'])
                last=next(i for i,u in enumerate(source['speech_units']) if u['unit_id']==clip['last_unit'])
                mask=((1 << (last-first+1))-1) << first
                self.assertTrue(all(not mask & old for old in masks))
                masks.append(mask)
                end=units[clip['last_unit']]['end_us']
                self.assertLessEqual(end, source['audio_duration_us'] + 34_000)

    def setUp(self):
        self.assets = [{'asset_id': 'asset_'+c*24, 'path': f'C:/{c}.mp4', 'width': 1080,
                        'height': 1920, 'video_duration_us': 12_000_000,
                        'audio_duration_us': 12_000_000, 'has_audio': True,
                        'frame_rate_mode': 'CFR', 'rotation': 0} for c in ('a', 'b')]
        self.task = task_spec('混剪带货，其他自主安排', [a['asset_id'] for a in self.assets], 'product_ad')
        self.sources = [{'asset_id': a['asset_id'], 'speech_units': [
            {'unit_id': f'u{i+1:03d}', 'start_us': i*4_000_000, 'end_us': (i+1)*4_000_000,
             'text': f'完整表达{i}。'} for i in range(2)],
            'frames': [{'requested_us': 2_000_000}, {'requested_us': 6_000_000}]} for a in self.assets]
        self.plan = {'schema_version': '1.0', 'title': '宠物碗带货', 'story': '展示设计、实际使用及清洗',
                     'clips': []}
        for s in self.sources:
            for i in range(2):
                self.plan['clips'].append({'asset_id': s['asset_id'], 'first_unit': f'u{i+1:03d}',
                    'last_unit': f'u{i+1:03d}', 'role': 'proof', 'reason': '可见结构与台词对应',
                    'frame_times_us': [2_000_000+i*4_000_000], 'heading': '实际展示',
                    'palette': 'mint', 'placement': 'upper'})

    def test_compiles_multiple_sources_and_native_editable_headings(self):
        t, rows, p = compile_ad(self.task, self.assets, self.sources, self.plan)
        self.assertEqual(t['duration_us'], 16_000_000)
        self.assertEqual(len(t['captions']), 4)
        self.assertEqual(t['captions'][0]['ad_style']['font'], 'source_han_cn')
        self.assertEqual(t['captions'][1]['ad_style']['font'], 'source_han_cn')
        self.assertEqual(t['source_map'][-1]['timeline_end_us'], 16_000_000)

    def test_rejects_hallucinated_evidence_units_and_resource_paths(self):
        for field, value in [('first_unit','u999'), ('frame_times_us',[3_000_000]),
                             ('palette','C:/unknown.exe'), ('heading','x'*13)]:
            p = copy.deepcopy(self.plan); p['clips'][0][field] = value
            with self.assertRaises(PlanIssue): compile_ad(self.task,self.assets,self.sources,p)

    def test_rejects_repeated_footage_and_single_source(self):
        p=copy.deepcopy(self.plan);p['clips'][-1]=p['clips'][0]
        with self.assertRaisesRegex(PlanIssue,'overlap'):
            compile_ad(self.task,self.assets,self.sources,p)
        p=copy.deepcopy(self.plan)
        for clip in p['clips']: clip['asset_id']=self.assets[0]['asset_id']
        with self.assertRaisesRegex(PlanIssue,'two different'):
            compile_ad(self.task,self.assets,self.sources,p)

    def test_preserves_sentence_fragments_and_excludes_cut_tail(self):
        cues=[{'start_us':0,'end_us':2_000_000,'text':'可以完整'},
              {'start_us':2_000_000,'end_us':4_000_000,'text':'拆开清洗。'},
              {'start_us':4_000_000,'end_us':5_000_000,'text':'没有说完'}]
        units=speech_units(self.assets[0],{'cues':cues})
        self.assertEqual(len(units),1)
        self.assertEqual(units[0]['text'],'可以完整拆开清洗。')

    def test_unsubstantiated_claims_cannot_pass_inside_selected_range_or_heading(self):
        sources=copy.deepcopy(self.sources)
        sources[0]['speech_units'][1]['text']='呵护肠胃。'
        p=copy.deepcopy(self.plan);p['clips'][0]['last_unit']='u002'
        with self.assertRaisesRegex(PlanIssue,'excluded'):
            compile_ad(self.task,self.assets,sources,p)
        p=copy.deepcopy(self.plan);p['clips'][0]['heading']='十来块好物'
        with self.assertRaisesRegex(PlanIssue,'claim'):
            compile_ad(self.task,self.assets,self.sources,p)
        for heading in ('慢食槽防抢食','干湿分离不串味','更卫生更放心'):
            p=copy.deepcopy(self.plan);p['clips'][0]['heading']=heading
            with self.assertRaisesRegex(PlanIssue,'claim'):
                compile_ad(self.task,self.assets,self.sources,p)

    def test_review_cannot_accept_unresolved_issue(self):
        class Provider:
            def _json(self,*args,**kwargs):
                return {'schema_version':'1.0','decision':'accept','issues':[
                    {'clip_index':1,'problem':'重复','suggested_fix':'换段'}],'limits':'静帧'}
        with self.assertRaisesRegex(PlanIssue,'unresolved'):
            timeline,candidates,_=compile_ad(self.task,self.assets,self.sources,self.plan)
            review_ad(Provider(),self.task,self.plan,candidates,timeline,[])

    def test_review_repairs_malformed_schema_without_accepting_it(self):
        class Provider:
            calls=0
            def _json(self,*args,**kwargs):
                self.calls+=1
                if self.calls==1:return {'decision':'accept','issues':[],'limits':[]}
                self.feedback=args[1]['validation_feedback']
                return {'schema_version':'1.0','decision':'uncertain','issues':[], 'limits':'Only sampled frames'}
        provider=Provider();timeline,candidates,_=compile_ad(self.task,self.assets,self.sources,self.plan)
        result=review_ad(provider,self.task,self.plan,candidates,timeline,[])
        self.assertEqual(result['decision'],'uncertain');self.assertEqual(provider.calls,2)
        self.assertIn('Malformed',provider.feedback['error'])
        self.assertFalse(result['continuous_video_reviewed'])

    def test_timing_options_exclude_forbidden_crossings_and_feedback_contains_rejected_plan(self):
        source=copy.deepcopy(self.sources[0])
        source['speech_units'].insert(1,{'unit_id':'blocked','start_us':4_000_000,'end_us':5_000_000,'text':'呵护肠胃。'})
        source['speech_units'][-1].update(start_us=5_000_000,end_us=9_000_000)
        evidence=planning_evidence([source])[0]
        self.assertEqual(len(evidence['measured_span_options']),2)
        self.assertEqual(evidence['measured_span_options'][1]['duration_ms'],4000)
        self.task['target']['duration_ms']=28000
        feedback=plan_feedback(self.task,self.sources,self.plan,PlanIssue('too short'))
        self.assertEqual(feedback['measured_total_ms'],16000)
        self.assertEqual(feedback['difference_to_target_ms'],12000)
        self.assertEqual(feedback['rejected_plan'],self.plan)


if __name__ == '__main__': unittest.main()
