import unittest
from PIL import ImageFont
from agent_core.caption_policy import configure, acceptance
from agent_core.narration_timing import schedule, retime
from agent_core.independent_export import _wrap_caption
from tests.test_subtitle_styles import FONT


class CaptionPolicyTests(unittest.TestCase):
    def test_measured_speech_can_follow_scene_anchors_without_changing_pcm_length(self):
        spans=schedule([2_000_000,1_000_000,2_000_000],12_000_000,anchors=[0,4_000_000,9_000_000])
        self.assertEqual([s['start_us'] for s in spans],[0,4_000_000,9_000_000])
        self.assertEqual([s['end_us']-s['start_us'] for s in spans],[2_000_000,1_000_000,2_000_000])
        tight=schedule([3_000_000,2_000_000],5_500_000,anchors=[0,5_000_000])
        self.assertLessEqual(tight[-1]['end_us'],5_400_000)
        self.assertGreaterEqual(tight[1]['start_us'],tight[0]['end_us']+120_000)

    def test_short_copy_is_not_retimed_to_speech(self):
        cues=[{'source_cue_id':'n1','text':'A complete spoken sentence.','start_us':0,'end_us':5_000_000}]
        captions=[{'text':'Cozy corner','timing_policy':'visual_highlight','start_us':0,'end_us':2_000_000}]
        speech,text=retime(cues,captions,[{'start_us':0,'end_us':4_000_000}])
        self.assertEqual(speech[0]['end_us'],4_000_000)
        self.assertEqual(text,captions)

    def test_latin_wrap_preserves_whole_words(self):
        text='Okay, my pup climbed in and would not come out.'
        lines=_wrap_caption(text,ImageFont.truetype(str(FONT),60),880)
        self.assertGreater(len(lines),1)
        self.assertEqual(' '.join(lines),text)

    def test_independent_copy_and_intentional_silence_pass_but_missing_requirement_fails(self):
        task=configure({'workflow':'custom','render_backend':'direct_ffmpeg','resolved_editing_options':{'text':True,'sticker':False}})
        snap={'duration':5_000_000,'materials':{'texts':[{'content':'Cozy corner'}]},'tracks':[
            {'type':'text','segments':[{'target_timerange':{'start':0,'duration':2_000_000}}]}]}
        report=acceptance(task,snap,{'narration_cues':[{'end_us':2_000_000,'text':'A full sentence about a cozy corner.'}]})
        self.assertEqual(report['status'],'passed')
        self.assertFalse(report['requires_verbatim_match'])
        self.assertTrue(report['editorial_observations'])
        snap['materials']['texts']=[]
        self.assertEqual(acceptance(task,snap,{})['status'],'failed')


if __name__=='__main__':unittest.main()
