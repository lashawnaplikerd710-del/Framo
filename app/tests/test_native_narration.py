import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_core import native_narration as n
from agent_core.task_store import digest


class NewNarrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.audio=self.root/'new.mp3';self.audio.write_bytes(b'native-sample')
        self.original={'duration':2000000,'tracks':[{'id':'video','type':'video','segments':[
            {'id':'v','material_id':'vm','volume':1,'target_timerange':{'start':0,'duration':2000000},
             'source_timerange':{'start':0,'duration':2000000}}]}]}
        self.snapshot=copy.deepcopy(self.original);self.snapshot['tracks'][0]['segments'][0]['volume']=0
        self.snapshot['tracks'] += [
            {'id':'voice','type':'audio','segments':[{'id':'a','material_id':'am','volume':1,'speed':1,
                'target_timerange':{'start':0,'duration':1000000},'source_timerange':{'start':0,'duration':1000000}}]},
            {'id':'caption','type':'text','segments':[{'id':'t','material_id':'tm',
                'target_timerange':{'start':0,'duration':1000000}}]}]
        self.snapshot['materials']={'audios':[{'id':'am','type':'text_to_audio','text_id':'tm',
            'duration':1000000,'resource_id':n.TONE_ID,'path':str(self.audio)}],
            'texts':[{'id':'tm','content':json.dumps({'text':'这是新文案'})}]}
        self.receipts=[{'text':'这是新文案','audio_segment_id':'a','text_segment_id':'t',
            'resource_id':n.TONE_ID,'sha256':digest(self.audio),'slot':{'start_us':0,'duration_us':2000000}}]

    def check_voice(self):
        return n.check_new_voice(self.snapshot,self.original,self.receipts,'old-voice','old-caption')

    def test_native_tts_linkage_and_full_source_pass(self):
        self.assertTrue(self.check_voice()['new_voice_from_text_verified'])

    def test_extracted_original_audio_cannot_be_called_new_voice(self):
        self.snapshot['materials']['audios'][0]['type']='extract_music'
        with self.assertRaisesRegex(n.NarrationFailure,'provenance'):self.check_voice()

    def test_shortened_source_range_is_rejected(self):
        self.snapshot['tracks'][1]['segments'][0]['source_timerange']['duration']=900000
        with self.assertRaisesRegex(n.NarrationFailure,'clipped'):self.check_voice()

    def test_native_speed_keeps_entire_generated_audio_and_requires_speed_object(self):
        r=self.receipts[0];r['requested_synthesis_speed']=1.1
        s=self.snapshot['tracks'][1]['segments'][0];s['speed']=1.1
        s['source_timerange']['duration']=1100000;s['extra_material_refs']=['native-speed']
        self.snapshot['materials']['audios'][0]['duration']=1100000
        self.snapshot['materials']['speeds']=[{'id':'native-speed','speed':1.1,'type':'speed'}]
        self.assertTrue(self.check_voice()['complete_audio_not_truncated'])
        self.snapshot['materials']['speeds'][0]['speed']=1
        with self.assertRaisesRegex(n.NarrationFailure,'speed_object'):self.check_voice()

    def test_original_speech_cannot_play_under_new_voice(self):
        self.snapshot['tracks'][0]['segments'][0]['volume']=.1
        with self.assertRaisesRegex(n.NarrationFailure,'not_muted'):self.check_voice()

    def test_changed_caption_rejected(self):
        self.snapshot['materials']['texts'][0]['content']=json.dumps({'text':'另一句'})
        with self.assertRaisesRegex(n.NarrationFailure,'caption_mismatch'):self.check_voice()

    def test_caption_layout_breaks_preserve_exact_synthesized_words(self):
        self.snapshot['materials']['texts'][0]['content']=json.dumps({'text':'这是\n新文案'})
        self.assertTrue(self.check_voice()['caption_matches_synthesis_input'])
        self.snapshot['materials']['texts'][0]['content']=json.dumps({'text':'这是\n旧文案'})
        with self.assertRaisesRegex(n.NarrationFailure,'caption_mismatch'):self.check_voice()

    def test_long_chinese_voice_caption_wraps_without_truncating_or_tiny_font(self):
        import os
        font=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/msyh.ttc'
        if not font.exists():self.skipTest('Windows Chinese font required')
        text='这个蝴蝶结宠物碗，先看它怎么组合。'
        result=n.layout_voice_caption({'key':'voice_1','text':text,'size':12,'x':0,'y':.7},font,{'width':1080,'height':1920})
        self.assertEqual(result['text'].replace('\n',''),text)
        self.assertEqual(result['line_count'],2)
        self.assertGreaterEqual(result['size'],8)
        self.assertFalse(any(line[0] in '，。' for line in result['text'].split('\n')))

    def test_tts_receipt_does_not_keep_task_tokens_or_signed_urls(self):
        value={'token':'secret','payload':json.dumps({'audio_subtitles':[{'text':'新口播','duration':1000,
            'speech_url':'https://signed.example','token':'secret','utterances':[{'word':'新','start':0}]}]})}
        text=json.dumps(n.safe_subtitles(value),ensure_ascii=False)
        self.assertIn('新口播',text);self.assertNotIn('secret',text);self.assertNotIn('signed.example',text)

    def test_membership_tone_stops_before_synthesis(self):
        with patch.object(n,'envelope',return_value={'items':[{'resource_id':n.TONE_ID,'title':n.TONE_TITLE,'is_vip':True}]}):
            with self.assertRaisesRegex(n.NarrationFailure,'not_explicitly_free'):n.resolve_tone(None,self.root)

    def test_native_tts_stays_out_of_import_bin(self):
        n.native_modules()
        from native_build import local_bin_entries
        self.assertEqual(local_bin_entries({'materials':{'audios':[{'type':'text_to_audio','path':'missing.mp3'}]}}),[])

    def test_desktop_main_timeline_is_selected_and_multiple_timelines_stop(self):
        draft=self.root/'draft';(draft/'Timelines/main').mkdir(parents=True)
        f=draft/'Timelines/project.json';f.write_text(json.dumps({'main_timeline_id':'main','timelines':[{'id':'main'}]}))
        with patch.object(n,'read_native',return_value={'native':True}) as read:
            self.assertTrue(n.read_editable_main(None,draft)['native']);self.assertEqual(read.call_args.args[1],draft/'Timelines/main')
        f.write_text(json.dumps({'main_timeline_id':'main','timelines':[{'id':'main'},{'id':'other'}]}))
        with self.assertRaisesRegex(n.NarrationFailure,'multiple_timelines'):n.read_editable_main(None,draft)


if __name__=='__main__':unittest.main()
