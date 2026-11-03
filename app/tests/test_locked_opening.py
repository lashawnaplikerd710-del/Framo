import copy
import unittest
from unittest.mock import Mock

from agent_core import locked_opening as lo, original_montage as om
from agent_core.commerce import compile_ad
from agent_core.desktop_workbench import default_instruction
from agent_core.workbench import Workbench
from agent_core.timeline_compiler import PlanIssue
from tests.test_long_video import fixture


def setup(duration=90000, count=2):
    _, assets, _, _ = fixture(count=count)
    task = Workbench._prepare_spec(None, {'workflow':om.WORKFLOW,
        'instruction':default_instruction(om.WORKFLOW),
        'asset_ids':[a['asset_id'] for a in assets], 'target_duration_ms':duration})
    sources=[]
    for asset in assets:
        sources.append({'asset_id':asset['asset_id'],'video_duration_us':120_000_000,
            'audio_duration_us':120_000_000,'has_audio':True,'speech_mode':'speech',
            'speech_units':[{'unit_id':f's{i:03d}','start_us':i*3_000_000+ (200_000 if i==0 else 0),
                'end_us':(i+1)*3_000_000,'text':'完整表达。'} for i in range(40)],
            'frames':[{'requested_us':i*1_000_000} for i in range(120)],'observations':[]})
    opening=lo.build(task,sources)
    ids=[f'{si}:{u["unit_id"]}' for si,s in enumerate(sources) for u in reversed(s['speech_units'])
         if s['asset_id']!=opening['asset_id'] or u['start_us']>=opening['end_us']]
    provider=Mock();provider._json.return_value={'title':'完整开头与主题混剪',
        'chapters':[{'title':'演示','unit_ids':ids}]}
    return task,assets,sources,provider,opening


class LockedOpeningTests(unittest.TestCase):
    def test_explicit_seven_output_batch_uses_seven_different_sources(self):
        task,_,sources,_,_=setup(count=10)
        task['instruction']+='\n本批七条成品的开头四十秒优先使用不同素材，开头不要重复。'
        chosen=[]
        for index in range(1,8):
            chosen.append(lo.build({**task,'batch_output_index':index},sources)['asset_id'])
        self.assertEqual(len(set(chosen)),7)
        self.assertEqual(chosen[3],lo.build({**task,'batch_output_index':4},sources)['asset_id'])

    def test_diverse_openings_exclude_identical_file_copies(self):
        task,_,sources,_,_=setup(count=9)
        for i,source in enumerate(sources):
            source['transcript']={'source_sha256':str(i//2)}
        task['instruction']+='\n本批成品开头使用不同素材。'
        chosen=[lo.build({**task,'batch_output_index':i},sources)['asset_id'] for i in range(1,6)]
        by_id={s['asset_id']:s for s in sources}
        self.assertEqual(len({by_id[k]['transcript']['source_sha256'] for k in chosen}),5)

    def test_old_batch_without_diversity_request_keeps_original_opening(self):
        task,_,sources,_,opening=setup(count=10)
        for i in range(1,8):
            self.assertEqual(lo.build({**task,'batch_output_index':i},sources)['asset_id'],opening['asset_id'])

    def test_default_brief_and_new_jobs_enable_hard_policy(self):
        task,_,_,_,opening=setup()
        self.assertEqual(task['opening_policy'],lo.POLICY)
        self.assertIn('前四十秒',task['instruction'])
        self.assertTrue(task['constraints']['allow_reorder'])
        self.assertEqual(opening['end_us'],42_000_000)

    def test_real_compiler_starts_at_zero_not_first_asr_token_and_tail_reorders(self):
        task,assets,sources,provider,opening=setup()
        plan=om.plan(provider,task,sources)
        timeline,_,_=compile_ad(task,assets,sources,plan)
        self.assertEqual(timeline['duration_us'],90_000_000)
        maps=timeline['source_map'];n=len(opening['parts'])
        self.assertEqual(maps[0]['source_start_us'],0)
        self.assertEqual(maps[n-1]['source_end_us'],42_000_000)
        self.assertGreater(n,8)
        for row in maps[:n]:
            self.assertEqual(row['asset_id'],opening['asset_id'])
            self.assertEqual(row['source_start_us'],row['timeline_start_us'])
            self.assertEqual(row['source_end_us'],row['timeline_end_us'])
        for row in maps[n:]:
            if row['asset_id']==opening['asset_id']:
                self.assertGreaterEqual(row['source_start_us'],42_000_000)
        self.assertEqual(len({r['asset_id'] for r in maps}),2)
        self.assertTrue(any(v['backward_source_jumps']>0 for v in
                            plan['original_montage_report']['tail_source_usage'].values()))
        payload=provider._json.call_args.args[1]
        self.assertEqual(payload['locked_opening']['remaining_duration_ms'],48000)

    def test_tampered_prefix_or_repeated_prefix_is_rejected(self):
        task,assets,sources,provider,opening=setup()
        plan=om.plan(provider,task,sources)
        bad=copy.deepcopy(plan);bad['clips'][0],bad['clips'][1]=bad['clips'][1],bad['clips'][0]
        with self.assertRaisesRegex(PlanIssue,'固定开头'):compile_ad(task,assets,sources,bad)
        bad=copy.deepcopy(plan);bad['clips'][-1]=copy.deepcopy(bad['clips'][0])
        with self.assertRaisesRegex(PlanIssue,'只能出现一次'):compile_ad(task,assets,sources,bad)
        bad=copy.deepcopy(plan);tail=bad['clips'][-1]
        tail.update(asset_id=opening['asset_id'],first_unit='s000',last_unit='s000',frame_times_us=[0])
        with self.assertRaisesRegex(PlanIssue,'不能重复'):compile_ad(task,assets,sources,bad)

    def test_short_inputs_and_target_fail_in_chinese_before_model(self):
        with self.assertRaisesRegex(ValueError,'四十秒'):setup(30000)
        task,_,sources,provider,_=setup()
        for s in sources:s['video_duration_us']=39_000_000
        with self.assertRaisesRegex(PlanIssue,'四十秒'):om.plan(provider,task,sources)
        provider._json.assert_not_called()

    def test_render_snapshot_keeps_opening_audio_continuous_and_decorates_it(self):
        from agent_core.direct_renderer import snapshot_from_timeline
        task,assets,sources,provider,opening=setup()
        timeline,_,_=compile_ad(task,assets,sources,om.plan(provider,task,sources))
        snapshot=snapshot_from_timeline(task,assets,timeline)
        video=next(t for t in snapshot['tracks'] if t['type']=='video')['segments']
        head=video[:len(opening['parts'])]
        self.assertTrue(all(s['fade_in_us']==0 and s['fade_out_us']==0 for s in head))
        self.assertTrue(all('transition' in s and 'effect' in s for s in head[:-1]))
        self.assertTrue(all(s['source_timerange']==s['target_timerange'] for s in head))

    def test_single_source_and_twenty_sources_have_same_prefix_contract(self):
        for count in (1,20):
            task,assets,sources,provider,opening=setup(count=count)
            plan=om.plan(provider,task,sources)
            timeline,_,_=compile_ad(task,assets,sources,plan)
            self.assertEqual(timeline['locked_opening']['end_us'],42_000_000)
            self.assertEqual(timeline['source_map'][0]['asset_id'],opening['asset_id'])

    def test_forty_second_target_needs_no_model_and_retains_exactly_forty(self):
        task,assets,sources,provider,opening=setup(40000)
        plan=om.plan(provider,task,sources)
        timeline,_,_=compile_ad(task,assets,sources,plan)
        self.assertEqual(timeline['duration_us'],40_000_000)
        provider._json.assert_not_called()

    def test_old_saved_jobs_are_not_reinterpreted(self):
        task,_,sources,_,_=setup();task.pop('opening_policy')
        self.assertIsNone(lo.build(task,sources))

    def test_automatic_duration_includes_locked_opening_once(self):
        task,assets,sources,provider,opening=setup()
        task['target']['duration_ms']=None
        plan=om.plan(provider,task,sources)
        timeline,_,_=compile_ad(task,assets,sources,plan)
        # Only the other source's leading ASR gap is absent; the selected
        # opening must include its gap and must not be subtracted twice.
        self.assertAlmostEqual(timeline['duration_us']/1e6,239.8,places=3)

    def test_automatic_single_forty_second_source_does_not_require_nonexistent_tail(self):
        task,assets,sources,provider,_=setup(count=1)
        task['target']['duration_ms']=None
        sources[0].update(video_duration_us=40_000_000,audio_duration_us=40_000_000)
        sources[0]['speech_units']=[u for u in sources[0]['speech_units'] if u['end_us']<=39_000_000]
        plan=om.plan(provider,task,sources)
        timeline,_,_=compile_ad(task,assets,sources,plan)
        self.assertEqual(timeline['duration_us'],40_000_000)
        provider._json.assert_not_called()


if __name__=='__main__':unittest.main()
