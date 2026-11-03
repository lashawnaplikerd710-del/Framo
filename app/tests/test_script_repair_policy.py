import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock
from agent_core import ad_narration
from agent_core.commerce import classify_script_review,NarrationPlanIssue

class ScriptRepairPolicyTests(TestCase):
    def test_actual_complex_request_shorthand_overrides_automatic_style(self):
        from agent_core.subtitle_styles import resolve_style
        style,_=resolve_style({'instruction':'日语卖点字幕，白字黑描边无底板','subtitle_style_seed':'seed'},
                              {'ad_style':{'palette':'cream'}})
        self.assertEqual(style['fill_color'],'#FFFFFF')
        self.assertEqual(style['background_color'],'#00000000')
    def test_optional_detail_does_not_block_but_facts_and_legacy_do(self):
        review={'decision':'revise','issues':['可以增加一个卖点','编造治病功效','旧格式具体错误'],
            'findings':[{'problem':'可以增加一个卖点','category':'optional_detail','quote':'入口','evidence':'只是建议补充'},
                        {'problem':'编造治病功效','category':'fact_error','quote':'治病','evidence':'未见依据'}]}
        out=classify_script_review(review)
        self.assertEqual(out['issues'],['编造治病功效','旧格式具体错误'])
        self.assertEqual(out['decision'],'revise')
        self.assertEqual(review['issues'][0],'可以增加一个卖点')
    def test_optional_only_accepts_but_uncertainty_never_dropped(self):
        review={'decision':'revise','issues':['换个短句'],'findings':[{'problem':'换个短句','category':'style','quote':'看看','evidence':'风格偏好'}]}
        self.assertEqual(classify_script_review(review)['decision'],'accept')
        self.assertEqual(classify_script_review({**review,'decision':'uncertain'})['decision'],'uncertain')
    def test_budget_survives_new_provider_and_cannot_reset_on_resume(self):
        with tempfile.TemporaryDirectory() as root:
            task={'_script_repair_ledger':str(Path(root)/'budget.json')}
            for _ in range(2):ad_narration.claim_repair(Mock(),task,{},NarrationPlanIssue)
            with self.assertRaisesRegex(NarrationPlanIssue,'两轮'):
                ad_narration.claim_repair(Mock(),task,{},NarrationPlanIssue)

    def test_same_interrupted_rewrite_identity_does_not_consume_another_round(self):
        with tempfile.TemporaryDirectory() as root:
            task={'_script_repair_ledger':str(Path(root)/'budget.json')}
            for _ in range(3):ad_narration.claim_repair(Mock(),task,{},NarrationPlanIssue,identity='same')
            ad_narration.claim_repair(Mock(),task,{},NarrationPlanIssue,identity='second')
            with self.assertRaises(NarrationPlanIssue):
                ad_narration.claim_repair(Mock(),task,{},NarrationPlanIssue,identity='third')
    def test_short_narration_allowed_unless_full_coverage_explicit(self):
        task={'render_backend':'direct_ffmpeg','narration_mode':'new_native','workflow':'crossborder_montage',
              'ad_narration_policy':ad_narration.POLICY,'caption_policy_version':'selling_highlights_v1','caption_mode':'highlights'}
        # The existing configure function chooses the actual active policy.
        task=ad_narration.configure(task)
        cues=[{'start_us':0,'end_us':3_600_000}]
        self.assertIsNone(ad_narration.pacing_issue(task,cues,12_000_000))
        task['instruction']='需要全程日语口播'
        # Test the enabled policy rather than relying on a legacy version string.
        from unittest.mock import patch
        with patch('agent_core.ad_narration.enabled',return_value=True):
            self.assertEqual(ad_narration.pacing_issue(task,cues,12_000_000)['kind'],'underfilled')
    def test_exhausted_budget_switches_to_reviewed_fallback_without_new_rewrite(self):
        import copy
        from unittest.mock import patch
        from agent_core import narration_timing
        from tests.test_auto_narration import AutoNarrationTests
        with tempfile.TemporaryDirectory() as root:
            service,job,fixture,task,plan,timeline=AutoNarrationTests().fixture(root)
            bounded={**task,'_script_repair_ledger':str(service.store.job_dir(job)/'auto-narration/repair-budget.json')}
            provider=Mock()
            for _ in range(2):ad_narration.claim_repair(provider,bounded,timeline,NarrationPlanIssue)
            candidate=copy.deepcopy(plan)
            candidate['clips'][0]['voiceover']='近くで見てみましょう。'
            overflow=narration_timing.NarrationDurationIssue({'available_us':15_000_000,'measured_us':22_000_000})
            with patch.object(service,'_run_direct_delivery',side_effect=[overflow,{'state':'COMPLETED'}]) as delivery, \
                 patch('agent_core.ad_narration.factual_candidates',return_value=[(candidate,{'requires_content_review':True})]), \
                 patch('agent_core.commerce.review_direct_script',return_value={'decision':'accept','issues':[]}) as review:
                result=service._deliver_direct_ad(job,task,fixture.assets,fixture.sources,timeline,plan,provider)
            self.assertEqual(result['state'],'COMPLETED')
            self.assertEqual(delivery.call_count,2)
            self.assertEqual(review.call_count,1)
            provider._json.assert_not_called()
