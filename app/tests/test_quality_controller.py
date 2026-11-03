import copy
import json
from pathlib import Path
import tempfile
import unittest

from agent_core.quality_controller import check_native_draft
from agent_core.task_store import digest


class NativeReadbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.media = Path(self.temp.name) / 'copied-source.mp4'
        self.media.write_bytes(b'original source')
        self.hash = digest(self.media)
        self.timeline = {'duration_us': 1_000_000,
                         'source_map': [{'source_segment_id': 'segment_0001',
                                         'source_start_us': 2_000_000,
                                         'source_end_us': 3_000_000,
                                         'timeline_start_us': 0,
                                         'timeline_end_us': 1_000_000}],
                         'captions': [{'source_cue_id': 'cue_0001', 'start_us': 0,
                                       'end_us': 1_000_000, 'text': '你好'}]}
        self.snapshot = {'duration': 1_000_000,
                         'tracks': [
                             {'type': 'video', 'segments': [
                                 {'material_id': 'video_1',
                                  'source_timerange': {'start': 2_000_000, 'duration': 1_000_000},
                                  'target_timerange': {'start': 0, 'duration': 1_000_000}}]},
                             {'type': 'text', 'segments': [
                                 {'material_id': 'text_1',
                                  'target_timerange': {'start': 0, 'duration': 1_000_000}}]}],
                         'materials': {'videos': [{'id': 'video_1', 'path': str(self.media)}],
                                       'texts': [{'id': 'text_1',
                                                  'content': json.dumps({'text': '你好'})}]}}

    def tearDown(self):
        self.temp.cleanup()

    def check(self, snapshot=None, editable=True):
        return check_native_draft(snapshot or self.snapshot, self.timeline,
                                  {'issues': [], 'scope': 'draft'},
                                  editable_captions=editable, source_sha256=self.hash)

    def test_exact_native_mapping_and_source_hash(self):
        self.assertEqual(self.check()['issues'], [])

    def test_rejects_wrong_source_media_even_if_times_match(self):
        self.media.write_bytes(b'different source')
        self.assertIn('video_material_hash_mismatch',
                      [item['code'] for item in self.check()['issues']])

    def test_rejects_wrong_clip_time_and_caption_text(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot['tracks'][0]['segments'][0]['source_timerange']['start'] += 1_000_000
        snapshot['materials']['texts'][0]['content'] = json.dumps({'text': '再见'})
        codes = [item['code'] for item in self.check(snapshot)['issues']]
        self.assertIn('video_time_mapping_mismatch', codes)
        self.assertIn('caption_mapping_mismatch', codes)

    def test_rejects_unexpected_caption_layer(self):
        self.assertIn('unexpected_text_segments',
                      [item['code'] for item in self.check(editable=False)['issues']])

    def test_embedded_source_audio_is_not_silently_muted(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot['materials']['videos'][0]['has_audio'] = True
        snapshot['tracks'][0]['segments'][0]['volume'] = 0
        result = check_native_draft(snapshot, self.timeline, {'issues': []},
                                    editable_captions=True, source_sha256=self.hash,
                                    source_has_audio=True)
        self.assertIn('embedded_audio_missing_or_muted',
                      [item['code'] for item in result['issues']])

    def test_montage_verifies_exact_source_identity_per_clip(self):
        snapshot = copy.deepcopy(self.snapshot)
        timeline = copy.deepcopy(self.timeline)
        timeline['source_map'][0]['asset_id'] = 'asset_expected'
        manifest = {'asset_expected': {'sha256': self.hash, 'has_audio': False},
                    'asset_other': {'sha256': 'different', 'has_audio': False}}
        checked = check_native_draft(snapshot, timeline, {'issues': []},
                                     editable_captions=True, source_manifest=manifest)
        self.assertEqual(checked['issues'], [])
        timeline['source_map'][0]['asset_id'] = 'asset_other'
        checked = check_native_draft(snapshot, timeline, {'issues': []},
                                     editable_captions=True, source_manifest=manifest)
        self.assertIn('video_material_hash_mismatch', [x['code'] for x in checked['issues']])

    def test_native_packaging_keeps_embedded_audio_and_checks_added_heading_separately(self):
        snap=copy.deepcopy(self.snapshot)
        main=snap['tracks'][0];main['id']='main';main['segments'][0].update(id='picture',volume=.8)
        snap['tracks'][1]['segments'][0]['id']='caption'
        snap['materials']['videos'][0]['has_audio']=True
        heading={'id':'headline','material_id':'heading','target_timerange':{'start':0,'duration':1_000_000}}
        snap['tracks'].append({'id':'titles','type':'text','segments':[heading]})
        snap['materials']['texts'].append({'id':'heading','content':json.dumps({'text':'卖点'})})
        report={'schema':'jianying-native-task-packaging-v1','original_tracks':[main],'items':[
            {'kind':'text','segment_id':'headline','request':{'text':'卖点','start_us':0,'duration_us':1_000_000}}]}
        def check():return check_native_draft(snap,self.timeline,{'issues':[]},editable_captions=True,
            source_sha256=self.hash,source_has_audio=True,packaging_report=report)
        self.assertEqual(check()['issues'],[])
        snap['materials']['texts'][-1]['content']=json.dumps({'text':'错误'})
        self.assertIn('native_packaging_object_mismatch',[i['code'] for i in check()['issues']])
        main['segments'][0]['volume']=0
        self.assertIn('embedded_audio_missing_or_muted',[i['code'] for i in check()['issues']])

    def test_packaging_allows_only_intentional_mute_and_detects_missing_or_shifted_voice(self):
        snap=copy.deepcopy(self.snapshot)
        main=snap['tracks'][0];main['id']='main';main['segments'][0].update(id='picture',volume=0)
        voice=copy.deepcopy(main['segments'][0]);voice.update(id='voice',volume=.8)
        snap['tracks'].append({'id':'voice_track','type':'audio','segments':[voice]})
        overlay=copy.deepcopy(main['segments'][0]);overlay['id']='overlay'
        snap['tracks'].append({'id':'overlay_track','type':'video','segments':[overlay]})
        pack={'original_tracks':[main], 'items':[{'role':'original_audio','segment_id':'voice'},
            {'role':'transitions','segment_id':'overlay'}]}
        audit={'issues':[{'code':'segment_muted','segment_id':'picture','severity':'info'},
                         {'code':'segment_muted','segment_id':'overlay','severity':'info'}]}
        def check():return check_native_draft(snap,self.timeline,audit,editable_captions=True,
            source_sha256=self.hash,source_has_audio=True,packaging_report=pack)
        self.assertEqual(check()['issues'],[])
        voice['source_timerange']['start']+=100000
        self.assertIn('separated_audio_mapping_mismatch',[i['code'] for i in check()['issues']])
        voice['source_timerange']['start']-=100000
        main['segments'][0]['volume']=1
        self.assertIn('double_source_audio',[i['code'] for i in check()['issues']])
        main['segments'][0]['volume']=0
        snap['tracks'][2]['segments']=[]
        self.assertIn('separated_audio_mapping_mismatch',[i['code'] for i in check()['issues']])


if __name__ == '__main__':
    unittest.main()
