import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import wave

from agent_core import installed_runtime, local_voice, workbench


class InstalledRuntimeTests(unittest.TestCase):
    def test_installed_output_folder_preserves_program_and_checkpoint_protection(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw).resolve()/'program'; root.mkdir()
            (root/'installed-profile.json').write_text('{"schema":"framo-installed-v1"}')
            app=workbench.Workbench.__new__(workbench.Workbench)
            app.root=root/'UserData'; app.root.mkdir()
            app.mutex=threading.RLock(); app.service=None; app.settings={'draft_root':''}
            app.settings_path=app.root/'settings.json'
            with patch.object(workbench,'ROOT',root):
                result=app.save_output_directory(str(app.root/'Exports'))
                self.assertTrue(result['saved'])
                for folder in (root,root/'agent_core',app.root,app.root/'tasks/jobs',app.root/'VoiceCache'):
                    with self.subTest(folder=folder), self.assertRaises(ValueError):
                        app.save_output_directory(str(folder))

    def test_legacy_does_not_change_environment(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {}, clear=True):
            installed_runtime.prepare(raw)
            self.assertNotIn('JIANYING_AGENT_STATE_DIR', os.environ)

    def test_all_installed_paths_follow_selected_unicode_location(self):
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {}, clear=True):
            root = Path(raw).resolve()/'安装目录 with spaces'; root.mkdir()
            (root/'installed-profile.json').write_text('{"schema":"framo-installed-v1"}')
            installed_runtime.prepare(root)
            for key in ('JIANYING_AGENT_STATE_DIR','FRAMO_VOICE_CACHE','TEMP','TMP','HF_HOME'):
                self.assertTrue(Path(os.environ[key]).is_relative_to(root/'UserData'))
            self.assertTrue(Path(os.environ['TEMP']).is_dir())

    def test_clean_profile_does_not_discover_legacy_editor(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            (root/'installed-profile.json').write_text('{"schema":"framo-installed-v1"}')
            (root/'gateway-endpoint.json').write_text('{"base_url":"https://example.com/v1"}')
            with patch.object(workbench,'ROOT',root), patch.object(workbench,'detect_install') as detect:
                settings = workbench.default_settings()
            self.assertEqual(settings,{'gateway_url':'https://example.com/v1','install_dir':'','draft_root':'','user_data_dir':''})
            detect.assert_not_called()

    def test_generated_voice_is_returned_from_separate_cache(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); library=root/'library'; library.mkdir(); cache=root/'UserData/VoiceCache'
            def fake_run(cmd, **kwargs):
                request=json.loads(Path(cmd[-1]).read_text(encoding='utf-8'))
                self.assertEqual(request['cache_root'],str(cache.resolve()))
                output=cache/'cache/audio/test.wav'; output.parent.mkdir(parents=True)
                with wave.open(str(output),'wb') as out:
                    out.setnchannels(1);out.setsampwidth(2);out.setframerate(24000);out.writeframes(b'\x00\x10'*24000)
                result={'items':[{'path':'cache/audio/test.wav','text':'Hello there.', 'locale':'en-US',
                                  'sha256':local_voice.sha256(output)}]}
                Path(request['result']).write_text(json.dumps(result),encoding='utf-8')
                return type('Result',(),{'returncode':0})()
            with patch.dict(os.environ,{'FRAMO_VOICE_CACHE':str(cache)}), \
                 patch.object(local_voice,'choose',return_value={'id':'example'}), \
                 patch.object(local_voice.subprocess,'run',side_effect=fake_run):
                rows=local_voice.generate(library,['Hello there.'],'en-US',root/'job')
            self.assertTrue(Path(rows[0]['path']).is_relative_to(cache))
            self.assertEqual(list(library.iterdir()),[])


if __name__ == '__main__':
    unittest.main()
