import copy
import unittest
from agent_core.packaging import validate_packaging, CATEGORIES
from agent_core.timeline_compiler import PlanIssue
import test_commerce
from agent_core.commerce import compile_ad


class PackagingTests(unittest.TestCase):
    def setUp(self):
        base=test_commerce.CommerceTests();base.setUp()
        self.timeline=compile_ad(base.task,base.assets,base.sources,base.plan)[0]
        self.plan={'schema_version':'1.0','decisions':{k:{'status':'applied','reason':'明确用途'} for k in CATEGORIES},
            'music':{'theme':'light','volume':.15},
            'stickers':[{'clip_index':1,'icon':'sparkle','anchor':'upper_left','offset_ms':300,'duration_ms':1000,'reason':'外观'}],
            'sfx':[{'clip_index':2,'kind':'tick','offset_ms':300,'volume':.2,'reason':'提示'}],
            'transitions':[{'before_clip':2,'duration_frames':6,'reason':'段落'}]}
        self.plan['decisions']['captions']={'status':'not_needed','reason':'保留已烧录字幕'}

    def test_all_layers_and_accurate_text_decisions(self):
        self.assertIs(validate_packaging(self.plan,self.timeline),self.plan)
        self.plan['decisions']['captions']['status']='applied'
        with self.assertRaisesRegex(PlanIssue,'editable text'): validate_packaging(self.plan,self.timeline)

    def test_applied_requires_real_events(self):
        for category in ('music','stickers','sfx','transitions'):
            with self.subTest(category=category):
                p=copy.deepcopy(self.plan);p[category]=None if category=='music' else []
                with self.assertRaisesRegex(PlanIssue,'disagree'): validate_packaging(p,self.timeline)

    def test_catalog_only_and_time_bounds(self):
        for category, field, value in [('stickers','icon','../../secret'),('sfx','volume',float('nan')),
            ('stickers','duration_ms',5000),('sfx','offset_ms',5000),('transitions','before_clip',1),
            ('transitions','duration_frames',30)]:
            with self.subTest(field=field):
                p=copy.deepcopy(self.plan);p[category][0][field]=value
                with self.assertRaises(PlanIssue):validate_packaging(p,self.timeline)

    def test_blocked_cannot_silently_deliver(self):
        self.plan['decisions']['music']['status']='blocked'
        with self.assertRaisesRegex(PlanIssue,'blocked'):validate_packaging(self.plan,self.timeline)

    def test_new_workflows_share_policy(self):
        from agent_core.task_service import task_spec
        for w in ('talking_head','interview','product_montage','product_ad'):
            self.assertEqual(task_spec('自主剪辑',['a','b'] if w.startswith('product') else ['a'],w)['packaging_policy'],'editable_layers_v1')

    def test_silent_sources_do_not_require_fake_narration(self):
        self.timeline['has_original_audio']=False
        with self.assertRaisesRegex(PlanIssue,'audio presence'):validate_packaging(self.plan,self.timeline)
        self.plan['decisions']['narration']={'status':'not_needed','reason':'素材没有音频流'}
        validate_packaging(self.plan,self.timeline)
