import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_core.native_resource_planner import validate_selection, plan_native, collect_candidates, CATALOG_SCHEMA
from agent_core.native_packaging import PackagingFailure, SCHEMA, KINDS


class ResourcePlannerTests(unittest.TestCase):
    def setUp(self):
        self.snapshot={'duration':6_000_000,'fps':30,'materials':{},'tracks':[{'type':'video','segments':[
            {'id':'a','target_timerange':{'start':0,'duration':3_000_000}},
            {'id':'b','target_timerange':{'start':3_000_000,'duration':3_000_000}}]}]}
        self.catalog={'schema':CATALOG_SCHEMA,'status':'official_candidates_verified','purpose':'commercial',
            'candidates':[{'kind':'music','resource_id':'123','exact_query_verified':True,
                          'entitlement':{'nonmember_metadata_verified':True,'commercial_field_value':True}}]}
        self.result={'plan':{'schema':SCHEMA,'purpose':'commercial','elements':[
            {'key':'bgm','kind':'music','resource_id':'123','start_us':0,'duration_us':6_000_000,'volume':.15}]},
            'decisions':{k:{'status':'applied' if k=='music' else 'not_needed','reason':'画面证据说明'} for k in KINDS}}

    def validate(self):return validate_selection(self.result,self.catalog,self.snapshot,'unused')

    def test_only_exact_queried_allowed_id_can_be_selected(self):
        self.validate()
        self.result['plan']['elements'][0]['resource_id']='999'
        with self.assertRaisesRegex(PackagingFailure,'unverified_resource'):self.validate()

    def test_membership_or_unknown_commercial_status_cannot_be_overridden_by_model(self):
        for field in ('nonmember_metadata_verified','commercial_field_value'):
            old=self.catalog['candidates'][0]['entitlement'][field]
            self.catalog['candidates'][0]['entitlement'][field]=None
            with self.assertRaisesRegex(PackagingFailure,'unverified_resource'):self.validate()
            self.catalog['candidates'][0]['entitlement'][field]=old
        self.result['plan']['purpose']='local_test'
        with self.assertRaisesRegex(PackagingFailure,'changed_resource_purpose'):self.validate()

    def test_category_omission_false_applied_and_blocked_cannot_pass(self):
        del self.result['decisions']['sound']
        with self.assertRaisesRegex(PackagingFailure,'every|all_native'):self.validate()
        self.result['decisions']['sound']={'status':'applied','reason':'虚构'}
        with self.assertRaisesRegex(PackagingFailure,'does_not_match'):self.validate()
        self.result['decisions']['sound']['status']='blocked'
        with self.assertRaisesRegex(PackagingFailure,'unavailable:sound'):self.validate()

    def test_wrong_timing_gets_returned_to_model_before_execution(self):
        invalid=copy.deepcopy(self.result);invalid['plan']['elements'][0]['duration_us']=7_000_000
        provider=Mock();provider._json.side_effect=[invalid,self.result]
        result=plan_native(provider,{'instruction':'剪辑','workflow':'product_ad'},self.catalog,self.snapshot,
                           'unused',{},[])
        self.assertEqual(result,self.result);self.assertEqual(provider._json.call_count,2)
        self.assertIn('validation_error',provider._json.call_args.args[1])

    def test_search_flags_alone_do_not_prove_rights_and_fallback_is_bounded(self):
        host=Mock();host.report={'status':'closed_after_healthy_connection','index_unchanged':True}
        client=Mock()
        result={'status':'metadata_received_unverified','candidates':[{'resource_id':'123','is_vip':False}]}
        query={'items':[{'resource_id':'123','paid_mode':'paid','is_commerce':True}]}
        with tempfile.TemporaryDirectory() as root, patch('agent_core.native_host.IsolatedNativeHost') as h, \
                patch('agent_core.native_cli.NativeCLI') as c, \
                patch('agent_core.native_resource_planner.QUERIES',{'music':('first','second')}), \
                patch('agent_core.native_resource_planner.search',return_value=result) as s, \
                patch('agent_core.native_resource_planner.envelope',return_value=query) as q:
            h.return_value.__enter__.return_value=host;c.return_value.__enter__.return_value=client
            catalog=collect_candidates('install',Path(root),allow_isolated_host=True)
            self.assertEqual(catalog['candidates'],[]);self.assertEqual(s.call_count,2)
            self.assertEqual(q.call_count,1)
            self.assertFalse(catalog['publishing_scope_verified'])

    def test_failed_category_is_kept_pending_without_discarding_verified_candidates(self):
        host=Mock();host.report={'status':'closed_after_healthy_connection','index_unchanged':True}
        client=Mock()
        music={'status':'metadata_received_unverified','candidates':[{'resource_id':'123'}]}
        failed={'status':'search_failed','reason':'temporary official search failure','candidates':[]}
        query={'items':[{'resource_id':'123','paid_mode':'free','subscribe_package':'',
                         'is_commerce':True,'source':1}]}
        with tempfile.TemporaryDirectory() as root, patch('agent_core.native_host.IsolatedNativeHost') as h, \
                patch('agent_core.native_cli.NativeCLI') as c, \
                patch('agent_core.native_resource_planner.QUERIES',
                      {'music':('music','music2'),'effect':('fx',)}), \
                patch('agent_core.native_resource_planner.search',
                      side_effect=lambda install, kind, query, **kwargs: music if kind == 'music' else failed), \
                patch('agent_core.native_resource_planner.envelope',return_value=query):
            h.return_value.__enter__.return_value=host;c.return_value.__enter__.return_value=client
            catalog=collect_candidates('install',Path(root),allow_isolated_host=True)
        self.assertEqual(catalog['status'],'partial_resources_pending')
        self.assertEqual([c['kind'] for c in catalog['candidates']],['music'])
        self.assertEqual([r['kind'] for r in catalog['pending_resources']],['effect'])
        self.assertEqual(sum(r['kind']=='effect' for r in catalog['searches']),2)
        self.assertTrue(all('attempt' in r for r in catalog['searches']))

    def test_pending_required_category_must_be_explicitly_blocked(self):
        catalog=copy.deepcopy(self.catalog)
        catalog['status']='partial_resources_pending'
        catalog['pending_resources']=[{'kind':'effect','status':'pending_resources'}]
        self.result['decisions']['effect']={'status':'blocked','reason':'官方特效搜索失败，待补资源'}
        self.validate_result = lambda: validate_selection(self.result,catalog,self.snapshot,'unused',
            required=('music','effect'))
        self.validate_result()
        self.result['decisions']['effect']={'status':'not_needed','reason':'暂时不用'}
        with self.assertRaisesRegex(PackagingFailure,'must_be_blocked'):
            self.validate_result()

    def test_planner_marks_missing_required_resource_instead_of_silently_dropping_it(self):
        catalog=copy.deepcopy(self.catalog)
        catalog['status']='partial_resources_pending'
        catalog['pending_resources']=[{'kind':'effect','status':'pending_resources'}]
        provider=Mock(); proposal=copy.deepcopy(self.result)
        proposal['decisions']['effect']={'status':'not_needed','reason':'画面暂时不需要'}
        provider._json.return_value=proposal
        result=plan_native(provider, {'instruction':'带货','workflow':'product_ad',
            'required_native_categories':['music','effect'], 'minimum_native_counts':{},
            'require_online_font':False, 'minimum_online_fonts':0}, catalog,
            self.snapshot, 'unused', {}, [])
        self.assertEqual(result['decisions']['effect']['status'],'blocked')
        self.assertIn('待补资源', result['decisions']['effect']['reason'])


if __name__=='__main__':unittest.main()
