"""Publishing credits survive audio planning, snapshots and MP4 delivery."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from agent_core import local_voice
from agent_core.direct_resources import build_direct_media_plan
from agent_core.direct_renderer import snapshot_from_timeline
from agent_core.independent_export import write_voice_credits
from tests import test_market_rules


EXPECTED = {
    'voicevox_metan': ('VOICEVOX:四国めたん', 'https://zunko.jp/con_ongen_kiyaku.html'),
    'voicevox_zundamon': ('VOICEVOX:ずんだもん', 'https://zunko.jp/con_ongen_kiyaku.html'),
    'voicevox_tsumugi': ('VOICEVOX:春日部つむぎ', 'https://tsumugi-official.studio.site/rule'),
    'voicevox_nana': ('VOICEVOX:春歌ナナ', 'https://nanahira.jp/haruka_nana/guideline.html'),
}
PENDING_CATALOG = Path(os.environ.get('FRAMO_PENDING_VOICE_CATALOG') or
    r'D:\JianyingAgentWork\RepairTests\20261017\japanese-voices\catalog.pending.json')


class VoiceAttributionDeliveryTests(unittest.TestCase):
    def test_generated_voice_credits_reach_snapshot_and_same_named_mp4_sidecar(self):
        # Exercise the actual resource planner, cached WAV preparation, snapshot
        # builder and delivery writer. Only neural inference is replaced here;
        # that expensive boundary has its own real synthesis validation.
        for voice_id, (attribution, terms) in EXPECTED.items():
            with self.subTest(voice_id=voice_id), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                speech = root/'fresh-speech.wav'
                with wave.open(str(speech), 'wb') as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(24000)
                    wav.writeframes(b'\x10\x10\xf0\xef'*12000)
                task, timeline = test_market_rules.MarketRuleTests().media('日本')
                task['direct_media'] = {'voice_id': voice_id}
                timeline.update(duration_us=6_000_000, captions=[],
                    source_map=[{'asset_id': 'asset_a', 'source_start_us': 0,
                        'source_end_us': 6_000_000, 'timeline_start_us': 0,
                        'timeline_end_us': 6_000_000}], narration_cues=[
                        {'start_us': 0, 'end_us': 3_000_000, 'text': 'ふわふわで、かわいいです。'},
                        {'start_us': 3_000_000, 'end_us': 6_000_000, 'text': 'お部屋にもなじみます。'}])
                entries = [{'path': str(speech), 'sha256': local_voice.sha256(speech),
                    'text': cue['text'], 'locale': 'ja-JP', 'voice_id': voice_id,
                    'cache_hit': False, 'review': {'status': '试听待确认', 'human_listened': False},
                    'attribution_required': True, 'attribution': attribution, 'terms_url': terms,
                    'license': 'VOICEVOX VVM terms; character attribution required',
                    'source': 'https://github.com/VOICEVOX/voicevox_vvm'}
                    for cue in timeline['narration_cues']]
                with patch.object(local_voice, 'generate', return_value=entries) as generate:
                    media = build_direct_media_plan(task, timeline, {}, root/'job',
                        {'direct_media': {'local_voice_library': str(root/'library')}})
                generate.assert_called_once()
                self.assertEqual(generate.call_args.kwargs['voice_id'], voice_id)
                self.assertFalse(media['pending'])
                expected = [{'voice_id': voice_id, 'attribution': attribution, 'terms_url': terms}]
                # Two spoken sentences use one speaker: publication credits
                # must appear once, rather than once for every narration cue.
                self.assertEqual(media['voice_attributions'], expected)
                saved_report = json.loads(Path(media['report_path']).read_text(encoding='utf-8'))
                self.assertEqual(saved_report['voice_attributions'], expected)
                assets = [{'asset_id': 'asset_a', 'path': str(root/'source.mp4'),
                    'sha256': 'a'*64, 'width': 720, 'height': 1280, 'has_audio': False}]
                snapshot = snapshot_from_timeline(task, assets, timeline, media_plan=media)
                self.assertEqual(snapshot['voice_attributions'], expected)
                before = copy.deepcopy(snapshot)
                output = root/'Framo-日区广告-001.mp4'
                sidecar = Path(write_voice_credits(snapshot, output))
                self.assertEqual(sidecar.name, 'Framo-日区广告-001-配音署名.txt')
                text = sidecar.read_text(encoding='utf-8-sig')
                self.assertIn('发布视频时', text)
                self.assertIn(attribution, text)
                self.assertIn(terms, text)
                self.assertEqual(text.count(attribution), 1)
                self.assertIn('此文件本身不代替发布时的署名', text)
                self.assertEqual(snapshot, before)

    @unittest.skipUnless(PENDING_CATALOG.is_file(), 'This optional asset audit requires the prepared voice catalog.')
    def test_real_pending_catalog_has_exact_credits_and_four_distinct_speakers(self):
        data = local_voice.catalog(PENDING_CATALOG.parent) if PENDING_CATALOG.name == 'catalog.json' else json.loads(
            PENDING_CATALOG.read_text(encoding='utf-8'))
        registered = {voice['id']: voice for voice in data['voices'] if voice['id'] in EXPECTED}
        self.assertEqual(set(registered), set(EXPECTED))
        self.assertEqual(len({voice['speaker_uuid'] for voice in registered.values()}), 4)
        for voice_id, (attribution, terms) in EXPECTED.items():
            with self.subTest(voice_id=voice_id):
                voice = registered[voice_id]
                self.assertEqual(voice['locale'], 'ja-JP')
                self.assertTrue(voice['attribution_required'])
                self.assertEqual(voice['attribution'], attribution)
                self.assertEqual(voice['terms_url'], terms)
                self.assertIn(attribution, local_voice.attribution_text(voice))
        self.assertEqual(data['defaults'], {'en-US': 'kokoro_af_heart', 'ja-JP': 'kokoro_jf_alpha'})

    def test_missing_required_credit_is_blocked_before_delivering_incomplete_sidecar(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)/'广告.mp4'
            for incomplete in [{'attribution': 'VOICEVOX:春歌ナナ'},
                               {'terms_url': EXPECTED['voicevox_nana'][1]}]:
                with self.subTest(credit=incomplete), self.assertRaisesRegex(ValueError, '署名'):
                    write_voice_credits({'voice_attributions': [incomplete]}, output)
            self.assertFalse(output.with_name('广告-配音署名.txt').exists())
            self.assertIsNone(write_voice_credits({}, output))


if __name__ == '__main__':
    unittest.main()
