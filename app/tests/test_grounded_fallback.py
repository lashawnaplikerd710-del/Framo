import copy
from unittest import TestCase
from unittest.mock import Mock
from agent_core import ad_narration
from agent_core.commerce import review_direct_script
from tests.test_market_rules import MarketRuleTests

class GroundedFallbackTests(TestCase):
    def fixture(self):
        f,task,plan=MarketRuleTests().fixture('日本')
        from agent_core.commerce import compile_ad
        timeline,_,_=compile_ad(task,f.assets,f.sources,plan)
        task=ad_narration.configure(task)
        row={'asset_id':f.assets[0]['asset_id'],'requested_us':0,'subject':'宠物窝',
             'visible_features':['入口','条纹'], 'description':'条纹宠物窝带有入口','confidence':'high'}
        provider=Mock()
        provider._json.return_value={'schema_version':'1.0','product':'ペットハウス',
            'narration_script':'入口のあるペットハウスです。','heading':'ペットハウスの入口',
            'claims':[{'evidence_id':'e1','quote':'条纹宠物窝带有入口'}]}
        return f,task,plan,timeline,row,provider

    def test_grounded_product_replaces_generic_placeholder_without_changing_shots(self):
        f,task,plan,timeline,row,provider=self.fixture()
        original=copy.deepcopy(plan)
        result=ad_narration.grounded_candidate(provider,task,plan,timeline,[row])
        self.assertEqual(len(result),1)
        candidate,report=result[0]
        self.assertEqual(candidate['_copy_policy'],'grounded_product_facts_v2')
        self.assertEqual(candidate['clips'][0]['heading'],'ペットハウスの入口')
        self.assertEqual(report['strategy'],'grounded_product_facts_v2')
        self.assertTrue(report['requires_content_review'])
        self.assertEqual(plan,original)
        self.assertEqual(candidate['clips'][0]['first_unit'],plan['clips'][0]['first_unit'])

    def test_invented_or_metadata_quotes_and_wrong_language_are_rejected(self):
        f,task,plan,timeline,row,provider=self.fixture()
        response=copy.deepcopy(provider._json.return_value)
        for quote in ('不存在的防水结构','e1',row['asset_id']):
            provider._json.return_value={**response,'claims':[{'evidence_id':'e1','quote':quote}]}
            self.assertEqual(ad_narration.grounded_candidate(provider,task,plan,timeline,[row]),[])
        provider._json.return_value={**response,'heading':'只写中文'}
        self.assertEqual(ad_narration.grounded_candidate(provider,task,plan,timeline,[row]),[])

    def test_conservative_review_uses_fixed_contract_without_invented_ad_structure(self):
        f,task,plan,timeline,row,provider=self.fixture()
        plan['_copy_policy']='grounded_product_facts_v2'
        provider._json.return_value={'schema_version':'1.0','decision':'accept','issues':[],
                                    'product':'ペットハウス','limits':'未听音频'}
        review_direct_script(provider,task,f.sources,plan,timeline)
        prompt=provider._json.call_args.args[0]
        self.assertIn('固定验收条件',prompt)
        self.assertIn('不能追加放入',prompt)
        self.assertNotIn('以信息完整为优先',prompt)

    def test_complete_short_alternative_is_selected_without_truncation(self):
        f,task,plan,timeline,row,provider=self.fixture()
        short=provider._json.return_value['narration_script']
        provider._json.return_value={**provider._json.return_value,
            'narration_script':'入口のあるペットハウスです。'*100,'alternatives':[short]}
        result=ad_narration.grounded_candidate(provider,task,plan,timeline,[row])
        self.assertEqual(result[0][0]['narration_script'],short)
        self.assertEqual(provider._json.call_count,1)

    def test_language_correction_retries_once_with_specific_feedback(self):
        f,task,plan,timeline,row,provider=self.fixture()
        valid=copy.deepcopy(provider._json.return_value)
        provider._json.side_effect=[{**valid,'heading':'仅使用中文'},valid]
        result=ad_narration.grounded_candidate(provider,task,plan,timeline,[row])
        self.assertEqual(len(result),1)
        self.assertEqual(provider._json.call_count,2)
        self.assertIn('语言错误',provider._json.call_args.args[1]['validation_feedback'])

    def test_invalid_response_has_bounded_generation_attempts(self):
        f,task,plan,timeline,row,provider=self.fixture()
        provider._json.return_value={}
        self.assertEqual(ad_narration.grounded_candidate(provider,task,plan,timeline,[row]),[])
        self.assertEqual(provider._json.call_count,2)

    def test_japanese_kanji_product_label_with_japanese_copy_is_valid(self):
        f,task,plan,timeline,row,provider=self.fixture()
        provider._json.return_value={**provider._json.return_value,'product':'毛布',
            'heading':'白い裏地の毛布','narration_script':'白い裏地の毛布です。'}
        self.assertEqual(len(ad_narration.grounded_candidate(provider,task,plan,timeline,[row])),1)
        provider._json.return_value={**provider._json.return_value,'heading':'白色毛毯'}
        self.assertEqual(ad_narration.grounded_candidate(provider,task,plan,timeline,[row]),[])

    def test_japanese_voice_with_chinese_new_captions_cannot_compile(self):
        from agent_core.commerce import validate_ad_script,NarrationPlanIssue
        f,task,plan,timeline,row,provider=self.fixture()
        candidate=ad_narration.grounded_candidate(provider,task,plan,timeline,[row])[0][0]
        candidate['clips'][0]['heading']='查看商品页的尺寸与详情'
        with self.assertRaisesRegex(NarrationPlanIssue,'屏幕文案未使用日语'):
            validate_ad_script(task,candidate,timeline)
