import unittest
from unittest.mock import Mock
from agent_core.direct_duration import feasible_schedules, plan_duration_bound_ad
from agent_core.timeline_compiler import PlanIssue


class DirectDurationTests(unittest.TestCase):
    def fixture(self,target=15_000):
        task={'workflow':'product_ad','narration_mode':'new_native','allow_single_source':True,
              'target':{'duration_ms':target,'tolerance_ms':3000}}
        source={'asset_id':'a','speech_mode':'visual','observations':[],
            'speech_units':[{'unit_id':f'v{i+1:03d}','start_us':i*3_000_000,'end_us':(i+1)*3_000_000,'text':''}
                            for i in range(8)],'frames':[{'requested_us':i*1_000_000} for i in range(24)]}
        return task,[source]

    def test_schedules_obey_measured_target_and_keep_choices_across_source(self):
        for target in (9000,15000,21000):
            task,sources=self.fixture(target)
            schedules=feasible_schedules(task,sources)
            self.assertTrue(schedules)
            ends=[]
            for schedule in schedules:
                self.assertEqual(schedule['measured_total_ms'],target)
                slots=schedule['slots'];self.assertLessEqual(len(slots),10)
                self.assertTrue(all(1000<=s['duration_ms']<=10000 for s in slots))
                self.assertTrue(all(a['end_us']<=b['start_us'] for a,b in zip(slots,slots[1:])))
                self.assertTrue(all(s['frame_times_us'] for s in slots))
                ends.append(slots[-1]['end_us'])
            self.assertIn(24_000_000,ends)

    def test_model_selects_schedule_but_cannot_change_its_times(self):
        task,sources=self.fixture();schedule=feasible_schedules(task,sources)[0]
        provider=Mock();provider._json.return_value={'schema_version':'1.0','title':'商品','story':'说明',
            'schedule_id':schedule['schedule_id'],'clips':[{'clip_index':i+1,'first_unit':'invented'} for i in range(len(schedule['slots']))]}
        result=plan_duration_bound_ad(provider,task,sources,{'validation_error':'outside target'})
        self.assertEqual([c['first_unit'] for c in result['clips']],[s['first_unit'] for s in schedule['slots']])
        self.assertTrue(provider._json.call_args.kwargs['require_images'])
        provider._json.return_value['schedule_id']='nonexistent'
        with self.assertRaises(PlanIssue):plan_duration_bound_ad(provider,task,sources,{})

    def test_no_feasible_schedule_is_not_fabricated(self):
        task,sources=self.fixture(60_000)
        self.assertEqual(feasible_schedules(task,sources),[])
        task,sources=self.fixture();sources[0]['speech_mode']='speech'
        self.assertEqual(feasible_schedules(task,sources),[])


if __name__=='__main__':unittest.main()
