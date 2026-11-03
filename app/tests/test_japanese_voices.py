import array
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import wave

from agent_core import local_voice


def speech_wav():
    target = io.BytesIO()
    with wave.open(target, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(array.array('h', [1000, -1000]*12000).tobytes())
    return target.getvalue()


class JapaneseVoiceTests(unittest.TestCase):
    def test_credits_required_only_for_licensed_voices(self):
        self.assertEqual(local_voice.attribution_text({'id': 'old'}), '')
        voice = {'id': 'new', 'label': '日语 · 示例', 'attribution_required': True}
        with self.assertRaisesRegex(ValueError, '署名'):
            local_voice.attribution_text(voice)
        voice.update(attribution='VOICEVOX:示例', terms_url='https://example.com/terms')
        self.assertIn('VOICEVOX:示例', local_voice.attribution_text(voice))
        self.assertIn('发布视频时', local_voice.attribution_text(voice))

    def test_worker_synthesizes_full_new_script_and_retains_credits_on_cache_hit(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            voice = {'id': 'voicevox_nana', 'engine': 'voicevox_nana', 'locale': 'ja-JP',
                     'speaker_id': 54, 'speaker_uuid': 'distinct-speaker',
                     'attribution_required': True, 'attribution': 'VOICEVOX:春歌ナナ',
                     'terms_url': 'https://nanahira.jp/haruka_nana/guideline.html'}
            data = {'schema_version': 1, 'defaults': {'ja-JP': voice['id']},
                    'voices': [voice], 'models': {'voicevox_nana': {
                        'files': {}, 'license': '署名必需', 'source': 'https://github.com/VOICEVOX'}}}
            (root/'catalog.json').write_text(json.dumps(data), encoding='utf-8')
            engine = Mock()
            engine.tts.return_value = speech_wav()
            request = {'root': str(root), 'cache_root': str(root/'generated'),
                       'locale': 'ja-JP', 'voice': voice['id'], 'texts': ['ふわふわで、かわいいです。']}
            with patch.object(local_voice, '_load_voicevox', return_value=engine) as loader:
                first = local_voice.synthesize_request(request)['items'][0]
                second = local_voice.synthesize_request(request)['items'][0]
            loader.assert_called_once()
            engine.tts.assert_called_once_with(request['texts'][0], 54)
            self.assertFalse(first['cache_hit'])
            self.assertTrue(second['cache_hit'])
            self.assertEqual(first['text'], request['texts'][0])
            self.assertEqual(first['attribution'], 'VOICEVOX:春歌ナナ')
            credit = root/'generated'/second['attribution_file']
            self.assertIn('VOICEVOX:春歌ナナ', credit.read_text(encoding='utf-8'))
            checks = local_voice.validate_wav(root/'generated'/first['path'])
            self.assertEqual(checks['duration_us'], 1000000)
            self.assertFalse(first['review']['human_listened'])

    def test_voice_model_speaker_mismatch_is_rejected(self):
        runtime = Mock()
        loaded = Mock()
        loaded.metas = [types.SimpleNamespace(speaker_uuid='other-speaker', styles=[])]
        model_file = Mock()
        model_file.open.return_value.__enter__ = Mock(return_value=loaded)
        model_file.open.return_value.__exit__ = Mock(return_value=False)
        module = types.ModuleType('voicevox_core.blocking')
        module.Onnxruntime = runtime
        module.OpenJtalk = Mock()
        module.Synthesizer = Mock()
        module.VoiceModelFile = model_file
        with tempfile.TemporaryDirectory() as raw, patch.dict(sys.modules, {'voicevox_core.blocking': module}):
            before = list(sys.path)
            try:
                with self.assertRaisesRegex(ValueError, '说话人'):
                    local_voice._load_voicevox(raw, {'python': 'python', 'onnxruntime': 'runtime.dll',
                        'dictionary': 'dictionary', 'voice_model': 'test.vvm'},
                        {'speaker_uuid': 'chosen-speaker', 'speaker_id': 54})
            finally:
                sys.path[:] = before
        module.Synthesizer.return_value.load_voice_model.assert_not_called()


if __name__ == '__main__':
    unittest.main()
