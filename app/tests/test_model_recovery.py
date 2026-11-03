import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent_core.model_recovery import ModelRecovery, model_preflight
from agent_core.provider import ProviderError, ProviderClient


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.recovery=ModelRecovery(self.root)
        self.client=Mock(endpoint='https://gateway.test/v1/chat/completions',model='deepseek-flash',total_timeout=10,
                         _recovery_protocol_verified=True)
        self.body=b'{"messages":[],"model":"deepseek-flash"}'

    def test_disconnect_recovers_without_second_submission(self):
        self.client._exchange.side_effect=[ProviderError('disconnected',category='connection'),
            b'{"state":"COMPLETED"}',b'complete']
        self.assertEqual(self.recovery.execute(self.client,self.body,'private'),b'complete')
        calls=self.client._exchange.call_args_list
        self.assertEqual(sum(c.args[0] is not None for c in calls),1)
        self.assertEqual(len({c.args[2] for c in calls}),1)
        self.client._exchange.reset_mock()
        self.assertEqual(ModelRecovery(self.root).execute(self.client,self.body,'private'),b'complete')
        self.client._exchange.assert_not_called()
        for p in self.root.glob('*.json'):
            self.assertNotIn('private',p.read_text());self.assertNotIn('messages',p.read_text())

    def test_confirmed_terminal_text_loss_regenerates_once_after_query(self):
        self.client.terminal_text_regeneration=True
        self.client._exchange.side_effect=[ProviderError('lost',category='connection'),
            b'{"state":"UNCERTAIN","error_code":"worker_ended_without_receipt"}',b'new text']
        self.assertEqual(self.recovery.execute(self.client,self.body,'private'),b'new text')
        calls=self.client._exchange.call_args_list
        self.assertIsNone(calls[1].args[0]);self.assertEqual(calls[0].args[2],calls[1].args[2])
        self.assertNotEqual(calls[0].args[2],calls[2].args[2])
        saved=json.loads(next(self.root.glob('*.json')).read_text())
        self.assertEqual(saved['state'],'COMPLETED');self.assertEqual(len(saved['terminal_regenerations']),1)

    def test_normal_deadline_has_one_status_only_grace_for_gateway_terminal_receipt(self):
        self.client.terminal_text_regeneration=True;self.client.total_timeout=200
        clock=[0]
        def exchange(body,*args,**kwargs):
            if body is not None and clock[0]==0:
                clock[0]=201;raise ProviderError('lost',category='connection')
            if body is None:
                clock[0]=211;return b'{"state":"UNCERTAIN","error_code":"worker_ended_without_receipt"}'
            return b'finished'
        self.client._exchange.side_effect=exchange
        with patch('agent_core.model_recovery.time.monotonic',side_effect=lambda:clock[0]):
            self.assertEqual(self.recovery.execute(self.client,self.body,'private'),b'finished')
        self.assertIn('terminal_status_grace',(self.root/'transport.jsonl').read_text())
        self.assertEqual(sum(c.args[0] is not None for c in self.client._exchange.call_args_list),2)

    def test_terminal_regeneration_limit_survives_resume(self):
        self.client.terminal_text_regeneration=True
        terminal=b'{"state":"UNCERTAIN","error_code":"worker_ended_without_receipt"}'
        lost=ProviderError('lost',category='connection')
        self.client._exchange.side_effect=[lost,terminal,lost,terminal]
        with self.assertRaises(ProviderError):self.recovery.execute(self.client,self.body,'private')
        self.assertEqual(sum(c.args[0] is not None for c in self.client._exchange.call_args_list),2)
        self.client._exchange.reset_mock();self.client._exchange.side_effect=[terminal]
        with self.assertRaises(ProviderError):ModelRecovery(self.root).execute(self.client,self.body,'private')
        self.assertEqual(self.client._exchange.call_count,1);self.assertIsNone(self.client._exchange.call_args.args[0])

    def test_unknown_outcome_and_tool_calls_are_never_regenerated(self):
        self.client.terminal_text_regeneration=True
        for body,status in [(b'{"messages":[],"tools":[{"type":"function"}]}',
                b'{"state":"UNCERTAIN","error_code":"worker_ended_without_receipt"}'),
                (self.body,b'{"state":"UNCERTAIN"}')]:
            self.client._exchange.reset_mock();self.client._exchange.side_effect=[ProviderError('lost',category='connection'),status]
            with self.assertRaises(ProviderError):self.recovery.execute(self.client,body,'private')
            self.assertEqual(sum(c.args[0] is not None for c in self.client._exchange.call_args_list),1)

    def test_tls_eof_on_status_probe_is_retried(self):
        client = Mock(endpoint=self.client.endpoint, model='deepseek-flash', total_timeout=10,
                      _recovery_protocol_verified=False)
        client._exchange.side_effect = [ProviderError('tls eof', category='tls',
                                                       details={'exception_type': 'SSLEOFError'}),
                                         b'{"recovery_protocol":1}', b'complete']
        with patch('agent_core.model_recovery.time.sleep'):
            self.assertEqual(self.recovery.execute(client, self.body, 'private'), b'complete')
        self.assertEqual(client._exchange.call_count, 3)
        self.assertEqual(client._exchange.call_args_list[0].kwargs['path'], '/status')
        self.assertEqual(client._exchange.call_args_list[1].kwargs['path'], '/status')

    def test_resume_queries_before_sending_and_keeps_id(self):
        self.client._exchange.side_effect=[ProviderError('lost',category='connection'),b'{"state":"UNCERTAIN"}']
        with self.assertRaises(ProviderError):self.recovery.execute(self.client,self.body,'private')
        old=json.loads(next(self.root.glob('*.json')).read_text())['request_id']
        self.client._exchange.reset_mock();self.client._exchange.side_effect=[b'{"state":"COMPLETED"}',b'finished']
        self.assertEqual(ModelRecovery(self.root).execute(self.client,self.body,'private'),b'finished')
        self.assertIsNone(self.client._exchange.call_args_list[0].args[0])
        self.assertEqual(self.client._exchange.call_args_list[0].args[2],old)

    def test_missing_record_reuses_same_id_but_old_gateway_does_not_retry(self):
        self.client._exchange.side_effect=[ProviderError('lost',category='connection'),
            ProviderError('missing',status_code=404,details={'code':'request_not_found'}),b'complete']
        self.recovery.execute(self.client,self.body,'private')
        self.assertEqual(len({c.args[2] for c in self.client._exchange.call_args_list}),1)
        other=Mock(endpoint=self.client.endpoint,model='deepseek-flash',total_timeout=10,_recovery_protocol_verified=False)
        other._exchange.side_effect=[b'{}',ProviderError('lost',category='connection')]
        with self.assertRaises(ProviderError):self.recovery.execute(other,b'other','private')
        self.assertEqual(sum(c.args[0] is not None for c in other._exchange.call_args_list),1)
        other._exchange.reset_mock()
        with self.assertRaisesRegex(ProviderError,'Legacy gateway'):self.recovery.execute(other,b'other','private')
        other._exchange.assert_not_called()

    def test_definite_overload_is_bounded_and_has_distinct_ids(self):
        self.client._exchange.side_effect=ProviderError('overloaded',status_code=503,retryable=True)
        with patch('agent_core.model_recovery.time.sleep'),self.assertRaises(ProviderError) as caught:
            self.recovery.execute(self.client,self.body,'private')
        self.assertEqual(self.client._exchange.call_count,3)
        self.assertEqual(len({c.args[2] for c in self.client._exchange.call_args_list}),3)
        self.assertFalse(caught.exception.retryable)

    def test_checkpoint_tampering_is_rejected(self):
        self.client._exchange.return_value=b'original';self.recovery.execute(self.client,self.body,'private')
        next(self.root.glob('*.response')).write_bytes(b'tampered')
        with self.assertRaisesRegex(ProviderError,'content changed'):
            self.recovery.execute(self.client,self.body,'private')

    def test_auth_change_never_reuses_another_grants_result(self):
        self.client._exchange.return_value=b'original'
        self.recovery.execute(self.client,self.body,'one');self.recovery.execute(self.client,self.body,'two')
        self.assertEqual(self.client._exchange.call_count,2)

    def test_real_model_probe_is_short_cached_and_profile_bound(self):
        cfg={'base_url':'https://gateway.test/v1','model':'deepseek-flash','api_key_env':'TEST_KEY'}
        client=ProviderClient(cfg,key_value='private')
        provider=Mock(client=client,config=cfg)
        provider._call.return_value={'message':{'content':'{"ok":true}'}}
        first=model_preflight(provider,self.root);second=model_preflight(provider,self.root)
        self.assertFalse(first['cached']);self.assertTrue(second['cached']);self.assertEqual(provider._call.call_count,1)
        client._key_value='changed';model_preflight(provider,self.root)
        self.assertEqual(provider._call.call_count,2)

    def test_truncated_health_probe_retries_once_without_caching_a_failed_probe(self):
        from agent_core.deepseek import ModelOutputError
        cfg={'base_url':'https://gateway.test/v1','model':'deepseek-flash','api_key_env':'TEST_KEY'}
        client=ProviderClient(cfg,key_value='private');old_limit=client.max_tokens
        error=ModelOutputError('length');error.finish_reason='length'
        provider=Mock(client=client,config=cfg)
        provider._call.side_effect=[error,{'message':{'content':'{"ok":true}'}}]
        result=model_preflight(provider,self.root)
        self.assertEqual(result['status'],'passed');self.assertEqual(provider._call.call_count,2)
        self.assertNotEqual(provider._call.call_args_list[0],provider._call.call_args_list[1])
        self.assertEqual(client.max_tokens,old_limit)
        # A filtering decision is not a transient formatting/length error.
        client._key_value='different';error.finish_reason='content_filter'
        provider._call.reset_mock();provider._call.side_effect=error
        with self.assertRaises(ModelOutputError):model_preflight(provider,self.root)
        self.assertEqual(provider._call.call_count,1);self.assertEqual(client.max_tokens,old_limit)


if __name__=='__main__':unittest.main()
