import copy
import tempfile
import unittest
from unittest.mock import patch

from agent_core.deepseek import DeepSeekProvider, ModelOutputError
from agent_core.provider import ProviderError
from agent_core.review_recovery import (ReviewRecoveryProvider, validate_review,
                                       excluded_units, validate_selected_units)


VALID = {'schema_version': '1.0', 'decision': 'uncertain', 'issues': [], 'limits': 'No audio listening'}
CONFIG = {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash',
          'api_key_env': 'TEST_KEY'}


class RecoveryTests(unittest.TestCase):
    def payload(self):
        return {'plan': {'clips': [{'heading': '放粮放水'}]},
                'selected_transcript': [{'segment_id': 's1', 'text': '原话'}],
                'source_map': [{'source_segment_id': 's1', 'source_start_us': 0}]}

    def test_zero_based_issue_is_rejected_not_silently_shifted(self):
        value = {**VALID, 'decision': 'revise', 'issues': [
            {'clip_index': 0, 'problem': '问题', 'suggested_fix': '修改'}]}
        with self.assertRaises(ModelOutputError): validate_review(value, 1)
        self.assertEqual(value['issues'][0]['clip_index'], 0)

    def test_requests_correct_number_and_preserves_model_decision(self):
        bad = {**VALID, 'decision': 'revise', 'issues': [{'clip_index': 0}]}
        original = self.payload(); before = copy.deepcopy(original)
        with tempfile.TemporaryDirectory() as tmp, patch.object(
                DeepSeekProvider, '_json', side_effect=[bad, VALID]) as request:
            result = ReviewRecoveryProvider(CONFIG, evidence_dir=tmp)._json('role', original)
            self.assertEqual(result, VALID)
            self.assertEqual(request.call_count, 2)
            sent = request.call_args.args[1]
            self.assertEqual(sent['review_clips'][0]['clip_index'], 1)
            self.assertIn('1..1', sent['response_validation_error'])
        self.assertEqual(original, before)

    def test_repeated_empty_answers_stop_after_three(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(DeepSeekProvider, '_json',
                side_effect=ProviderError('Provider returned no usable content or tool call.')) as request:
            with self.assertRaises(ProviderError):
                ReviewRecoveryProvider(CONFIG, evidence_dir=tmp)._json('role', self.payload())
            self.assertEqual(request.call_count, 3)

    def test_auth_error_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(DeepSeekProvider, '_json',
                side_effect=ProviderError('Authentication failed', category='authentication')) as request:
            with self.assertRaises(ProviderError):
                ReviewRecoveryProvider(CONFIG, evidence_dir=tmp)._json('role', self.payload())
            self.assertEqual(request.call_count, 1)

    def test_cannot_turn_unresolved_issues_into_acceptance(self):
        with self.assertRaises(ModelOutputError):
            validate_review({**VALID, 'decision': 'accept', 'issues': [
                {'clip_index': 1, 'problem': '重复', 'suggested_fix': '换段'}]}, 1)

    def test_interval_cannot_hide_a_blocked_claim_between_clean_endpoints(self):
        sources = [{'asset_id': 'a', 'speech_units': [
            {'unit_id': 'u1', 'text': '有一定高度'},
            {'unit_id': 'u2', 'text': '呵护气管和颈椎'},
            {'unit_id': 'u3', 'text': '可拆洗'}]}]
        blocked = excluded_units(sources)
        plan = {'clips': [{'asset_id': 'a', 'first_unit': 'u1', 'last_unit': 'u3'}]}
        with self.assertRaises(ModelOutputError):
            validate_selected_units(plan, sources, blocked)
        plan['clips'][0]['last_unit'] = 'u1'
        validate_selected_units(plan, sources, blocked)

    def test_far_away_frame_evidence_is_rejected_with_specific_nearby_times(self):
        sources = [{'asset_id': 'a', 'speech_units': [
            {'unit_id': 'u1', 'text': '可拆洗', 'start_us': 16_000_000, 'end_us': 18_000_000}],
            'observations': [{'frame_times_us': [17_000_000, 25_000_000]}]}]
        plan = {'clips': [{'asset_id': 'a', 'first_unit': 'u1', 'last_unit': 'u1',
                           'frame_times_us': [25_000_000]}]}
        with self.assertRaisesRegex(ModelOutputError, '17000000'):
            validate_selected_units(plan, sources, set())
        plan['clips'][0]['frame_times_us'] = [17_000_000]
        validate_selected_units(plan, sources, set())


if __name__ == '__main__': unittest.main()
