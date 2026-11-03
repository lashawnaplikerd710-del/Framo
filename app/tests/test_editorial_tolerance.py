import copy,unittest
from agent_core.commerce import classify_script_review
from agent_core.market_rules import profile_for

class EditorialToleranceTests(unittest.TestCase):
 def fixture(self):
  task={'instruction':'温柔、自然的居家分享口吻','workflow':'crossborder_montage','render_backend':'direct_ffmpeg','market_rules':profile_for('日本')}
  plan={'narration_script':'ロングラグ。縁のパイピングまで、詳しくは商品ページで。','clips':[{'heading':'広げてもやわらかな質感'},{'heading':'詳しくは商品ページで'}]}
  problems=[('fact_error','広げてもやわらかな質感','工厂场景不属于居家分享语境，会让观众产生场景混淆'),('fact_error','詳しくは商品ページで','结尾出现彩色、蓝色、白色地毯，与深灰色地毯外观不同'),('incomplete_sentence','縁のパイピングまで、詳しくは商品ページで。','日语句子不完整，不够自然')]
  review={'decision':'revise','issues':[p[2] for p in problems],'product':'深灰色长毛地毯','findings':[{'category':c,'quote':q,'problem':p,'evidence':p} for c,q,p in problems]}
  sources=[{'asset_id':'a','observations':[{'frames':[{'requested_us':0,'subject':'灰色长毛地毯','confidence':'high'},{'requested_us':1,'subject':'蓝色长毛地毯与栏杆','confidence':'high'}]}]}]
  timeline={'source_map':[{'asset_id':'a','source_start_us':0,'source_end_us':2}]*2}
  return task,plan,review,sources,timeline
 def test_editorial_issues_are_retained_as_suggestions_without_rewriting_copy(self):
  t,p,r,s,tl=self.fixture();original=copy.deepcopy(p)
  out=classify_script_review(r,task=t,plan=p,sources=s,timeline=tl)
  self.assertEqual(out['decision'],'accept');self.assertEqual(out['issues'],[])
  self.assertEqual(len(out['suggestions']),3);self.assertEqual(p,original)
 def test_explicit_scene_and_colour_requirements_still_block(self):
  t,p,r,s,tl=self.fixture();t['instruction']='全片只用居家场景，不能切换颜色'
  out=classify_script_review(r,task=t,plan=p,sources=s,timeline=tl)
  self.assertEqual(out['decision'],'revise');self.assertEqual(len(out['issues']),2)
 def test_different_product_kind_is_not_accepted_as_colour_variation(self):
  t,p,r,s,tl=self.fixture();s[0]['observations'][0]['frames'][1]['subject']='蓝色吸尘器'
  out=classify_script_review(r,task=t,plan=p,sources=s,timeline=tl)
  self.assertIn(r['issues'][1],out['issues'])
 def test_factual_claim_and_unknown_findings_are_not_silently_dropped(self):
  t,p,r,s,tl=self.fixture();r['issues']+=['材质和防滑承诺无依据','未知问题'];r['findings']+=[{'category':'fact_error','quote':'広げてもやわらかな質感','problem':'材质和防滑承诺无依据','evidence':'没有证据'}]
  out=classify_script_review(r,task=t,plan=p,sources=s,timeline=tl)
  self.assertEqual(out['issues'],['材质和防滑承诺无依据','未知问题'])
 def test_legacy_strict_classifier_does_not_change_without_context(self):
  t,p,r,s,tl=self.fixture();self.assertEqual(classify_script_review(r)['decision'],'revise')
 def japanese_colour_fixture(self):
  t,p,r,s,tl=self.fixture()
  p['clips'][0]['heading']='リビングになじむ、北欧の落ち着き'
  problem='口播「深灰の長毛ラグ」とクリップ7の灰白（ライトグレー）の地毯では、色が異なり、色の一貫性が保たれていない。'
  r.update(product='北欧风长毛地毯（深灰色ロングラグ）',issues=[problem],findings=[{'category':'fact_error','quote':p['clips'][0]['heading'],'problem':problem,'evidence':'深灰色地毯と灰白仿毛皮毯の映像'}])
  s[0]['observations'][0]['frames'][1]['subject']='灰白仿毛皮毯'
  return t,p,r,s,tl
 def test_japanese_colour_review_on_non_cta_heading_is_only_a_suggestion(self):
  t,p,r,s,tl=self.japanese_colour_fixture();original=copy.deepcopy(p)
  out=classify_script_review(r,task=t,plan=p,sources=s,timeline=tl)
  self.assertEqual(out['decision'],'accept');self.assertEqual(out['suggestions'],r['issues']);self.assertEqual(p,original)
 def test_japanese_colour_review_with_non_slip_claim_still_blocks(self):
  t,p,r,s,tl=self.japanese_colour_fixture();r['issues'][0]+=' 滑りにくい加工の主張も映像で確認できない。';r['findings'][0]['problem']=r['issues'][0]
  out=classify_script_review(r,task=t,plan=p,sources=s,timeline=tl)
  self.assertEqual(out['decision'],'revise');self.assertEqual(out['issues'],r['issues'])
 def test_japanese_explicit_same_colour_requirement_still_blocks(self):
  t,p,r,s,tl=self.japanese_colour_fixture();t['instruction']='同じ色だけを使う'
  self.assertEqual(classify_script_review(r,task=t,plan=p,sources=s,timeline=tl)['decision'],'revise')
 def test_generic_product_suffix_does_not_prove_same_kind(self):
  t,p,r,s,tl=self.japanese_colour_fixture();r['product']='榨汁器';s[0]['observations'][0]['frames'][0]['subject']='灰色榨汁器';s[0]['observations'][0]['frames'][1]['subject']='白色吸尘器'
  self.assertEqual(classify_script_review(r,task=t,plan=p,sources=s,timeline=tl)['decision'],'revise')
 def test_colour_only_legacy_review_can_use_exact_current_quote_and_visual_evidence(self):
  t,p,r,s,tl=self.japanese_colour_fixture();r.pop('findings')
  p['clips'][-1]['heading']='サイズと詳細は商品ページで'
  r['issues']=['新字幕标题「サイズと詳細は商品ページで」（尺寸与详情请见商品页）挂在工厂铺展灰白/浅色地毯的镜头上，但前面5个镜头展示的是深灰/灰黑色长毛地毯，色彩呈现明显不一致，容易让观众误以为是两款不同商品，建议改为不指认具体颜色的收束文案或补充「カラー展開は商品ページで」']
  out=classify_script_review(r,task=t,plan=p,sources=s,timeline=tl)
  self.assertEqual(out['decision'],'accept');self.assertEqual(out['suggestions'],r['issues'])
 def test_legacy_review_without_exact_quote_or_with_hard_claim_still_blocks(self):
  for suffix in ('，还有防滑功效承诺无依据','，材质无法确认','，尺寸与实物不符'):
   t,p,r,s,tl=self.japanese_colour_fixture();r.pop('findings');r['issues']=['「リビングになじむ、北欧の落ち着き」色彩不一致'+suffix]
   self.assertEqual(classify_script_review(r,task=t,plan=p,sources=s,timeline=tl)['decision'],'revise')
  t,p,r,s,tl=self.japanese_colour_fixture();r.pop('findings');r['issues']=['旧字幕「不存在的文案」色彩不一致']
  self.assertEqual(classify_script_review(r,task=t,plan=p,sources=s,timeline=tl)['decision'],'revise')
 def test_closing_cta_does_not_require_product_in_every_final_frame(self):
  t,p,r,s,tl=self.fixture();p['clips'][-1]['role']='closing'
  issue='最后一镜的口播与字幕引导查看商品详情，但画面主体是未铺地毯的客厅木地板，与商品展示明显不对应'
  r.update(issues=[issue],findings=[{'category':'fact_error','quote':'詳しくは商品ページで','problem':issue,'evidence':'最后一镜为客厅'}])
  self.assertEqual(classify_script_review(r,task=t,plan=p,sources=s,timeline=tl)['decision'],'accept')
  t['instruction']='结尾必须展示商品'
  self.assertEqual(classify_script_review(r,task=t,plan=p,sources=s,timeline=tl)['decision'],'revise')
 def test_automatic_heading_translation_preserves_narration_shots_and_repair_budget(self):
  from agent_core.ad_narration import repair_heading_language
  from agent_core.commerce import NarrationPlanIssue
  from unittest.mock import Mock
  t,p,r,s,tl=self.fixture();p['clips'][1]['heading']='尺寸与颜色，可在商品页确认';before=copy.deepcopy(p)
  provider=Mock();provider._json.return_value={'caption_fixes':[{'clip_index':2,'heading':'色とサイズは商品ページで'}]}
  out=repair_heading_language(provider,t,p,NarrationPlanIssue)
  self.assertEqual(p,before);self.assertEqual(out['narration_script'],p['narration_script']);self.assertEqual(out['clips'][0],p['clips'][0]);self.assertEqual(out['clips'][1]['heading'],'色とサイズは商品ページで');provider._json.assert_called_once()
 def test_failed_translation_is_bounded_and_actionable(self):
  from agent_core.ad_narration import repair_heading_language
  from agent_core.commerce import NarrationPlanIssue
  from unittest.mock import Mock
  t,p,r,s,tl=self.fixture();p['clips'][1]['heading']='中文文案';provider=Mock();provider._json.return_value={'caption_fixes':[]}
  with self.assertRaisesRegex(NarrationPlanIssue,'无需修改完整旁白'):repair_heading_language(provider,t,p,NarrationPlanIssue)
  self.assertEqual(provider._json.call_count,2)
