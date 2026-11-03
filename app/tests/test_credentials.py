import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
import uuid

from agent_core.credential_store import (TARGET, CredentialError, credential_environment,
    delete_key, eligible_provider, load_key, prompt_and_save, save_key, validate_key)
from agent_core.credentials import save_from_pipe

CONFIG = {'api_key_env': 'DEEPSEEK_API_KEY', 'base_url': 'https://api.deepseek.com'}


class CredentialTests(unittest.TestCase):
    def test_password_dialog_pipe_saves_without_echoing_input(self):
        secret='synthetic-dialog-paste-key'
        out=io.StringIO()
        with patch('agent_core.credentials.save_key') as save, contextlib.redirect_stdout(out):
            result=save_from_pipe(io.StringIO(secret))
        save.assert_called_once_with(secret)
        self.assertTrue(result['saved'])
        self.assertNotIn(secret,str(result)+out.getvalue())

    def test_password_dialog_pipe_rejects_oversized_and_multiline_input(self):
        for invalid in ('x'*1025, 'synthetic-key\nsecond-key'):
            with patch('agent_core.credentials.save_key') as save:
                with self.assertRaises(CredentialError): save_from_pipe(io.StringIO(invalid))
                save.assert_not_called()

    def test_saved_key_loaded_in_memory_then_removed_even_on_error(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch('agent_core.credential_store.load_key', return_value='synthetic-saved-key'), \
             patch('agent_core.credential_store.prompt_and_save') as prompt:
            with self.assertRaisesRegex(RuntimeError, 'fixture'):
                with credential_environment(CONFIG) as source:
                    self.assertEqual(source, 'windows_credential_manager')
                    self.assertEqual(os.environ['DEEPSEEK_API_KEY'], 'synthetic-saved-key')
                    raise RuntimeError('fixture')
            self.assertNotIn('DEEPSEEK_API_KEY', os.environ)
            prompt.assert_not_called()

    def test_existing_environment_wins_and_is_not_persisted(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY':'synthetic-environment-key'}, clear=True), \
             patch('agent_core.credential_store.load_key') as load, \
             patch('agent_core.credential_store.save_key') as save:
            with credential_environment(CONFIG) as source:
                self.assertEqual(source, 'environment')
            self.assertEqual(os.environ['DEEPSEEK_API_KEY'], 'synthetic-environment-key')
            load.assert_not_called();save.assert_not_called()

    def test_saved_key_is_never_loaded_for_custom_endpoint_or_override(self):
        for url in ('https://other.test', 'http://api.deepseek.com',
                    'https://api.deepseek.com.evil.test', 'https://api.deepseek.com:444',
                    'https://api.deepseek.com/?token=x'):
            with patch.dict(os.environ, {'DEEPSEEK_BASE_URL':url}, clear=True), \
                 patch('agent_core.credential_store.load_key') as load:
                with credential_environment(CONFIG) as source:
                    self.assertEqual(source,'unavailable')
                load.assert_not_called()

    def test_first_interactive_setup_saves_once_and_does_not_print_secret(self):
        key='synthetic-hidden-prompt-key'
        out=io.StringIO()
        with patch.dict(os.environ, {}, clear=True), \
             patch('agent_core.credential_store.load_key', return_value=None), \
             patch('sys.stdin.isatty', return_value=True), \
             patch('getpass.getpass', return_value=key), \
             patch('agent_core.credential_store.save_key') as save, contextlib.redirect_stdout(out):
            with credential_environment(CONFIG, interactive=True):
                self.assertEqual(os.environ['DEEPSEEK_API_KEY'],key)
            save.assert_called_once_with(key)
        self.assertNotIn(key,out.getvalue())

    def test_prompt_does_not_fall_back_to_echoed_pipe_input(self):
        with patch('sys.stdin.isatty', return_value=False), patch('getpass.getpass') as getpass:
            with self.assertRaises(CredentialError): prompt_and_save()
            getpass.assert_not_called()

    def test_invalid_keys_are_rejected_without_echo(self):
        for key in ('', 'x', 'synthetic-key\nsecret', '界'*20, 'x'*513):
            with self.assertRaises(CredentialError) as ctx: validate_key(key)
            if len(key)>2: self.assertNotIn(key,str(ctx.exception))

    @unittest.skipUnless(sys.platform=='win32', 'Windows Credential Manager integration')
    def test_actual_windows_store_survives_process_exit_updates_and_deletes(self):
        target=TARGET+'/test/'+uuid.uuid4().hex
        fixture='synthetic-windows-credential-fixture'
        self.addCleanup(lambda: delete_key(target=target))
        save_key(fixture,target=target)
        code=('import sys; from agent_core.credential_store import load_key; '
              'v=load_key(target=sys.argv[1]); '
              'print("MATCH" if v=="synthetic-windows-credential-fixture" else "MISMATCH")')
        child=subprocess.run([sys.executable,'-c',code,target],cwd=Path(__file__).resolve().parents[1],
                             capture_output=True,text=True,timeout=10)
        self.assertEqual(child.returncode,0)
        self.assertEqual(child.stdout.strip(),'MATCH')
        self.assertNotIn(fixture,child.stdout+child.stderr)
        save_key('synthetic-replacement-value',target=target)
        self.assertEqual(load_key(target=target),'synthetic-replacement-value')
        self.assertTrue(delete_key(target=target))
        self.assertIsNone(load_key(target=target))
        self.assertFalse(delete_key(target=target))


if __name__=='__main__': unittest.main()
