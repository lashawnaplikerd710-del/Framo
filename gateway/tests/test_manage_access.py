"""Isolated grant edits; never contacts Cloudflare or reads real credentials."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('manage_access', Path(__file__).resolve().parents[1]/'scripts/manage_access.py')
manage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)


class QuotaManagementTests(unittest.TestCase):
    def test_half_year_max_quota_without_extra_lifetime_cap(self):
        from datetime import datetime,timezone
        started=datetime.now(timezone.utc)
        with patch.object(manage,'HERE',self.root), \
             patch.object(sys,'argv',['manage_access.py','issue','--id','halfyear-max',
                 '--days','182','--units','10000']), \
             patch.object(manage.subprocess,'run',return_value=SimpleNamespace(returncode=0)), \
             patch.object(manage,'show_code'),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(manage.main(),0)
        rows=json.loads(self.registry.read_text('utf-8'))
        self.assertEqual(rows[:-1],self.clients)
        self.assertEqual(rows[-1]['daily_units'],10000)
        self.assertNotIn('max_requests',rows[-1])
        self.assertAlmostEqual((datetime.fromisoformat(rows[-1]['expires_at'])-started).total_seconds(),182*86400,delta=5)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root/'private').mkdir()
        self.registry = self.root/'private/grants.json'
        self.clients = [dict(id='owner-test', token_hash='f'*64, enabled=False,
                             expires_at='2000-01-01T00:00:00Z', daily_units=200,
                             max_concurrent=1, models=['deepseek-flash']),
                        dict(id='friend-01', token_hash='e'*64, enabled=True,
                             expires_at='2099-01-01T00:00:00Z', daily_units=80,
                             max_concurrent=1, models=['deepseek-flash'])]
        self.registry.write_text(json.dumps(self.clients), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def execute(self, units='10000', returncode=0):
        output = io.StringIO()
        with patch.object(manage,'HERE',self.root), \
             patch.object(sys,'argv',['manage_access.py','quota','--id','owner-test','--units',units]), \
             patch.object(manage.subprocess,'run',return_value=SimpleNamespace(returncode=returncode)) as run, \
             contextlib.redirect_stdout(output):
            result = manage.main()
        return result, run, output.getvalue()

    def test_quota_only_preserves_token_expiry_disabled_state_and_other_grants(self):
        result, run, output = self.execute()
        self.assertEqual(result,0)
        updated = json.loads(self.registry.read_text('utf-8'))
        expected = [{**self.clients[0],'daily_units':10000},self.clients[1]]
        self.assertEqual(updated,expected)
        self.assertEqual(json.loads(run.call_args.kwargs['input']),expected)
        self.assertNotIn('f'*64,output)
        self.assertNotIn('DEEPSEEK_API_KEY',str(run.call_args.args))

    def test_upload_failure_preserves_registry_and_pending_evidence(self):
        before=self.registry.read_bytes()
        with self.assertRaisesRegex(RuntimeError,'not confirmed'):
            self.execute(returncode=1)
        self.assertEqual(self.registry.read_bytes(),before)
        self.assertEqual(json.loads((self.root/'private/grants.pending.json').read_text('utf-8'))[0]['daily_units'],10000)

    def test_invalid_quota_is_rejected_before_upload(self):
        for value in ('0','10001'):
            with self.assertRaisesRegex(ValueError,'units must be'):
                self.execute(units=value)
        self.assertEqual(json.loads(self.registry.read_text('utf-8')),self.clients)

    def test_one_day_fifty_total_requests_issue_preserves_other_grants(self):
        from datetime import datetime,timezone
        import hashlib
        started=datetime.now(timezone.utc)
        output=io.StringIO()
        with patch.object(manage,'HERE',self.root), \
             patch.object(sys,'argv',['manage_access.py','issue','--id','one-day-trial',
                 '--days','1','--units','750','--max-requests','50']), \
             patch.object(manage.subprocess,'run',return_value=SimpleNamespace(returncode=0)), \
             patch.object(manage,'show_code') as show,contextlib.redirect_stdout(output):
            self.assertEqual(manage.main(),0)
        updated=json.loads(self.registry.read_text(encoding='utf-8'))
        self.assertEqual(updated[:-1],self.clients)
        row=updated[-1];token=show.call_args.args[1]
        self.assertEqual(row['max_requests'],50);self.assertEqual(row['daily_units'],750)
        self.assertEqual(row['token_hash'],hashlib.sha256(token.encode()).hexdigest())
        lifetime=(datetime.fromisoformat(row['expires_at'])-started).total_seconds()
        self.assertTrue(86399<=lifetime<=86405)
        self.assertNotIn(token,output.getvalue())


if __name__ == '__main__': unittest.main()
