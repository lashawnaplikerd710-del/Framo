import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import Mock, patch

from agent_core import market_rules
from agent_core.commerce import (compile_ad, plan_ad, validate_ad_script,
    NarrationPlanIssue, rewrite_direct_voiceover, review_direct_script)
from agent_core.editing_rules import apply_rules
from agent_core.direct_resources import build_direct_media_plan
from agent_core.workbench import Workbench
from tests import test_commerce, test_task_service


def spec(country='日本', instruction='按类别规则剪辑', assets=None):
    return Workbench._prepare_spec(None, {'workflow':'crossborder_montage',
        'target_country':country, 'instruction':instruction, 'asset_ids':assets or ['asset_a']})


class MarketRuleTests(unittest.TestCase):
    def test_new_profiles_have_distinct_language_tone_and_source(self):
        for country, locale, tone in [('日本','ja-JP','温柔'),('美国','en-US','轻快')]:
            s=spec(country)
            self.assertEqual(s['narration_locale'],locale)
            self.assertEqual(s['target']['aspect_ratio'],'source')
            self.assertEqual(s['narration_mode'],'new_native')
            self.assertEqual(s['transcription_language'],'auto')
            self.assertFalse(s['copy_only']); self.assertTrue(s['mute_source_audio'])
            self.assertIn(tone,market_rules.prompt(s))
            self.assertEqual(s['market_rules']['source_thread'],market_rules.SOURCE_THREAD)
            self.assertIn('待补资源',market_rules.prompt(s))
            self.assertIsNone(s['target']['duration_ms'])

    def test_explicit_no_voice_and_horizontal_win_without_mutating_defaults(self):
        s=spec('美国','横屏，不要口播，保留原声')
        out=apply_rules(s,{'schema_version':'1.0','overrides':{
            'narration':{'value':'none','quote':'不要口播'},
            'mute_source_audio':{'value':False,'quote':'保留原声'}},'unsupported':[]})
        self.assertEqual(out['target']['aspect_ratio'],'16:9')
        self.assertEqual(out['narration_mode'],'preserve_source')
        self.assertTrue(out['copy_only']);self.assertFalse(out['mute_source_audio'])
        self.assertEqual(s['narration_mode'],'new_native')

    def test_one_asset_market_job_passes_real_task_store(self):
        f=test_task_service.TaskServiceTests();f.setUp()
        try:
            p=f.root/'source.mp4';p.write_bytes(b'isolated')
            aid=f.service.register_asset(p)
            for country in ('日本','美国'):
                job=f.service.create_job(spec(country,assets=[aid]))
                saved=f.service.get_job(job)['task_spec']
                self.assertEqual(saved['narration_mode'],'new_native')
                self.assertEqual(saved['market_rules']['country'],country)
        finally:f.tearDown()

    def test_old_saved_prompt_first_task_stays_text_only(self):
        s=spec();s.pop('market_rules');s['editing_defaults']['narration']='none'
        s.update(narration_mode='text_only',copy_only=True)
        out=apply_rules(s,{'schema_version':'1.0','overrides':{},'unsupported':[]})
        self.assertEqual(out['narration_mode'],'text_only')
        self.assertNotIn('market_rules',out)

    def fixture(self,country):
        f=test_commerce.CommerceTests();f.setUp()
        s=spec(country,assets=[a['asset_id'] for a in f.assets])
        p=copy.deepcopy(f.plan)
        lines=(['見て、この寝顔。','クッションは取り出せます。','お部屋にもなじむ色合い。','サイズは商品ページで。']
               if country=='日本' else ['Okay, this is their room now.', 'Tie the curtains open or let them down.',
                                      'Look at them tucked inside.', 'Check sizes on the product page.'])
        for i,(c,line) in enumerate(zip(p['clips'],lines)):
            c['voiceover']=line
            c['heading']=['くつろぎ空間','取り出せるクッション','お部屋に合う色','商品詳細へ'][i] if country=='日本' else ['Their cozy corner','Open or closed','Tucked inside','See sizes'][i]
        for source in f.sources:source['observations']=[]
        return f,s,p

    def test_compiler_localizes_voice_captions_and_actual_time_ranges(self):
        for country in ('日本','美国'):
            f,s,p=self.fixture(country)
            t,_,_=compile_ad(s,f.assets,f.sources,p)
            self.assertEqual([c['text'] for c in t['narration_cues']],[c['voiceover'] for c in p['clips']])
            self.assertEqual(t['captions'][0]['text'],p['clips'][0]['heading'])
            self.assertNotIn('narration_cue_id',t['captions'][0])
            self.assertEqual(t['captions'][0]['end_us'],t['source_map'][0]['timeline_end_us'])
            self.assertTrue(t['copy_localization']['voiceover_supported'])
            self.assertEqual(t['duration_us'],16_000_000)

    def test_user_can_request_verbatim_subtitles_or_leave_a_visual_shot_unlabelled(self):
        from agent_core.caption_policy import configure
        f,s,p=self.fixture('美国')
        s=configure({**s,'instruction':'逐字字幕'})
        t,_,_=compile_ad(s,f.assets,f.sources,p)
        self.assertEqual(t['captions'][0]['text'],p['clips'][0]['voiceover'])
        self.assertIn('narration_cue_id',t['captions'][0])
        s=configure({**s,'instruction':'不要逐字字幕'})
        p['clips'][1]['heading']=''
        p['clips'][1]['voiceover']=''
        t,_,_=compile_ad(s,f.assets,f.sources,p)
        self.assertEqual(len(t['captions']),3)
        self.assertEqual(len(t['narration_cues']),3)

    def test_content_repair_can_correct_a_wrong_heading_without_reselecting_shots(self):
        import json
        f,s,p=self.fixture('美国')
        t,_,_=compile_ad(s,f.assets,f.sources,p)
        provider=Mock()
        provider._json.return_value={'schema_version':'1.0','clips':[
            {'clip_index':i+1,'voiceover':c['voiceover'],**({'heading':'Cozy pet nook'} if i==0 else {})}
            for i,c in enumerate(p['clips'])]}
        repaired,_=rewrite_direct_voiceover(provider,s,f.sources,p,t,json.dumps({'issues':['Correct the first heading']}))
        self.assertEqual(repaired['clips'][0]['heading'],'Cozy pet nook')
        self.assertEqual(repaired['clips'][0]['first_unit'],p['clips'][0]['first_unit'])
        self.assertEqual(repaired['clips'][1:],p['clips'][1:])

    def test_language_and_timing_validation_reject_bad_scripts(self):
        s=spec('美国');t={'source_map':[{'timeline_start_us':0,'timeline_end_us':4_000_000}]}
        p={'clips':[{'heading':'Cozy corner','voiceover':'Okay, this is their room now.'}]}
        validate_ad_script(s,p,t)  # Would fail the old Chinese letter counter.
        p['clips'][0]['voiceover']='这是中文口播'
        with self.assertRaisesRegex(NarrationPlanIssue,'目标语言'):validate_ad_script(s,p,t)
        p['clips'][0]['voiceover']=' '.join(['room']*30)
        with self.assertRaisesRegex(NarrationPlanIssue,'words'):validate_ad_script(s,p,t)
        p['clips'][0]['voiceover']='ふわふわ、くつろげるね。'
        validate_ad_script(spec('日本'),p,t)  # All-kana speech is readable Japanese.

    def test_planning_repair_and_review_use_same_market_profile(self):
        for country in ('日本','美国'):
            f,s,p=self.fixture(country);provider=Mock();provider._json.return_value=p
            plan_ad(provider,s,f.sources)
            prompt=provider._json.call_args.args[0]
            self.assertIn(s['market_rules']['tone'],prompt)
            self.assertNotIn('不要生成口播脚本',prompt)
            self.assertNotIn('新中文',prompt)
            evidence=provider._json.call_args.args[1]['sources'][0]['measured_span_options'][0]
            self.assertIn('recommended_speech_units',evidence)
            self.assertNotIn('recommended_voice_characters',evidence)
            t,_,_=compile_ad(s,f.assets,f.sources,p)
            provider._json.return_value={'schema_version':'1.0','clips':[
                {'clip_index':i,'heading':c['heading'],'voiceover':c['voiceover']} for i,c in enumerate(p['clips'],1)]}
            repaired,_=rewrite_direct_voiceover(provider,s,f.sources,p,t,'检查口播')
            self.assertEqual(repaired,p)
            self.assertIn(s['market_rules']['tone'],provider._json.call_args.args[0])
            provider._json.return_value={'schema_version':'1.0','decision':'accept','issues':[],
                                         'product':'pet house','limits':'not listened'}
            review=review_direct_script(provider,s,f.sources,p,t)
            self.assertFalse(review['audio_listened'])
            self.assertIn(s['market_rules']['tone'],provider._json.call_args.args[0])

    def media(self,country='日本'):
        s=spec(country,'不要音乐，不要音效')
        s=apply_rules(s,{'schema_version':'1.0','overrides':{
            'music':{'value':False,'quote':'不要音乐'},'sound':{'value':False,'quote':'不要音效'}},'unsupported':[]})
        text='見て、この寝顔。' if country=='日本' else 'Look at their cozy corner.'
        return s,{'duration_us':3_000_000,'source_map':[],
                  'narration_cues':[{'start_us':0,'end_us':3_000_000,'text':text}]}

    def test_missing_localized_voice_stays_pending_without_calling_chinese_tts(self):
        for country in ('日本','美国'):
            s,t=self.media(country)
            with tempfile.TemporaryDirectory() as raw, patch('agent_core.direct_resources.synthesize_windows_sapi') as synth:
                r=build_direct_media_plan(s,t,{},Path(raw),{})
                synth.assert_not_called()
                self.assertEqual(r['report']['tts']['status'],'PENDING_RESOURCES')
                self.assertIn(s['narration_locale'],r['pending'][0]['reason'])
                self.assertFalse(r['tracks'])

    def test_reviewed_audio_is_bound_to_script_language_and_hash(self):
        s,t=self.media()
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);audio=root/'voice.wav'
            with wave.open(str(audio),'wb') as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(16000)
                wav.writeframes(b'\1\0'*16000)
            sha=hashlib.sha256(audio.read_bytes()).hexdigest()
            entry={'path':str(audio),'sha256':sha,'text':t['narration_cues'][0]['text'],'locale':'ja-JP',
                   'review':{'reviewer':'isolated fixture','naturalness_passed':True,
                             'pronunciation_passed':True,'sentence_complete':True}}
            config={'direct_media':{'localized_voiceovers':{'ja-JP':[entry]}}}
            with patch('agent_core.direct_resources.synthesize_windows_sapi') as synth:
                r=build_direct_media_plan(s,t,{},root/'job',config)
                self.assertFalse(r['pending']);self.assertEqual(r['report']['tts']['status'],'PROVIDED')
                synth.assert_not_called()
            self.assertEqual(hashlib.sha256(audio.read_bytes()).hexdigest(),sha)
            for key,bad in [('text','old script'),('locale','en-US'),('sha256','invalid'),('review',{})]:
                broken=copy.deepcopy(config);broken['direct_media']['localized_voiceovers']['ja-JP'][0][key]=bad
                r=build_direct_media_plan(s,t,{},root/key,broken)
                self.assertTrue(r['pending']);self.assertFalse(r['tracks'])


if __name__=='__main__':unittest.main()
