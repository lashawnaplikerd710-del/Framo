import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from agent_core import local_voice
from agent_core.direct_resources import build_direct_media_plan
from tests import test_market_rules


class LocalVoiceTests(unittest.TestCase):
    def test_long_chinese_chunks_preserve_all_text_and_blank_lines(self):
        text=('这是长视频口播，保留每个标点与数字。\n\n第二段文字，继续完整表达。'*80)
        chunks=local_voice.speech_chunks(text)
        self.assertEqual(''.join(chunks),text)
        self.assertTrue(all(0<len(part)<=160 for part in chunks))

    def test_catalog_rejects_duplicate_and_wrong_locale(self):
        with tempfile.TemporaryDirectory() as raw:
            path=Path(raw)/'catalog.json'
            data={'schema_version':1,'voices':[{'id':'one','locale':'en-US'}], 'defaults':{'en-US':'one'}}
            path.write_text(json.dumps(data))
            self.assertEqual(local_voice.choose(raw,'en-US')['id'],'one')
            with self.assertRaises(ValueError):local_voice.choose(raw,'ja-JP','one')
            data['voices']*=2;path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):local_voice.catalog(raw)

    def test_path_and_script_language_rejected_before_process(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.assertRaises(ValueError):local_voice.inside(raw,'../outside')
            with patch.object(local_voice,'choose',return_value={'id':'one'}), patch.object(local_voice.subprocess,'run') as run:
                with self.assertRaises(ValueError):local_voice.generate(raw,['これは日本語です。'],'en-US',raw)
                run.assert_not_called()

    def test_silence_is_not_success(self):
        with tempfile.TemporaryDirectory() as raw:
            wav=Path(raw)/'empty.wav'
            with wave.open(str(wav),'wb') as out:
                out.setnchannels(1);out.setsampwidth(2);out.setframerate(24000);out.writeframes(b'\0\0'*24000)
            with self.assertRaises(ValueError):local_voice.validate_wav(wav)

    def test_generated_voice_integrates_without_faking_human_review(self):
        s,t=test_market_rules.MarketRuleTests().media('美国')
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);source=root/'voice.wav'
            with wave.open(str(source),'wb') as out:
                out.setnchannels(1);out.setsampwidth(2);out.setframerate(24000);out.writeframes(b'\x00\x10'*24000)
            entry={'path':str(source),'sha256':local_voice.sha256(source),'text':t['narration_cues'][0]['text'],
                   'locale':'en-US','voice_id':'example','review':{'status':'试听待确认','human_listened':False},'cache_hit':False}
            config={'direct_media':{'local_voice_library':str(root)}}
            with patch.object(local_voice,'generate',return_value=[entry]) as generate, patch('agent_core.direct_resources.synthesize_windows_sapi') as sapi:
                result=build_direct_media_plan(s,t,{},root/'job',config)
                generate.assert_called_once();sapi.assert_not_called()
            self.assertFalse(result['pending']);self.assertEqual(result['report']['tts']['status'],'COMPLETED')
            self.assertFalse(result['report']['tts']['human_listened'])
            self.assertEqual(result['report']['tts']['voice_id'],'example')
            self.assertEqual(local_voice.sha256(source),entry['sha256'])

    def test_failure_is_pending_without_wrong_language_fallback(self):
        s,t=test_market_rules.MarketRuleTests().media('日本')
        with tempfile.TemporaryDirectory() as raw, patch.object(local_voice,'generate',side_effect=ValueError('模型指纹不一致')), patch('agent_core.direct_resources.synthesize_windows_sapi') as sapi:
            result=build_direct_media_plan(s,t,{},Path(raw),{'direct_media':{'local_voice_library':raw}})
            self.assertIn('指纹',result['pending'][0]['reason']);self.assertFalse(result['tracks']);sapi.assert_not_called()

    def test_bad_explicit_audio_is_not_silently_replaced(self):
        s,t=test_market_rules.MarketRuleTests().media('日本')
        with tempfile.TemporaryDirectory() as raw, patch.object(local_voice,'generate') as generate:
            result=build_direct_media_plan(s,t,{},Path(raw),{'direct_media':{'local_voice_library':raw,'localized_voiceovers':{'ja-JP':[]}}})
            self.assertTrue(result['pending']);generate.assert_not_called()


if __name__=='__main__':unittest.main()
