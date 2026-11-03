import json
import subprocess
import tempfile
from pathlib import Path
from unittest import TestCase
from agent_core.local_executor import MediaInspector
from agent_core.media_normalize import normalize
from agent_core.task_store import digest

FFMPEG=Path('D:/JianyingAgentWork/Toolchain/ffmpeg.exe')

class VFRNormalizationTests(TestCase):
    def test_delayed_original_audio_stays_delayed_after_conversion(self):
        import array,math
        inspector=MediaInspector()
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);source=root/'delayed-audio.mp4'
            subprocess.run([str(FFMPEG),'-v','error','-y','-f','lavfi','-i',
                'testsrc2=size=160x288:rate=30:duration=4','-itsoffset','0.5','-f','lavfi','-i',
                'sine=frequency=750:sample_rate=48000:duration=5.5','-vf',
                "setpts='if(lt(N,60),N/(15*TB),(4+(N-60)/30)/TB)'",'-fps_mode','vfr',
                '-c:v','libx264','-preset','ultrafast','-c:a','aac',str(source)],check=True,capture_output=True)
            row=inspector.inspect(source);self.assertEqual(row['frame_rate_mode'],'VFR')
            out=normalize(row,inspector,root/'cache',FFMPEG)
            pcm=subprocess.run([str(FFMPEG),'-v','error','-i',out['path'],'-vn','-ac','1',
                '-ar','16000','-f','s16le','-'],check=True,capture_output=True).stdout
            samples=array.array('h',pcm)
            rms=lambda values:math.sqrt(sum(v*v for v in values)/len(values))/32768
            self.assertLess(rms(samples[:4800]),.001)
            self.assertGreater(rms(samples[12800:16000]),.01)
            self.assertTrue(out['has_audio'])

    def test_cfr_is_unchanged(self):
        row={'frame_rate_mode':'CFR'}
        self.assertIs(normalize(row,None,None,None),row)

    def test_real_vfr_audio_silent_cache_and_original_integrity(self):
        inspector=MediaInspector()
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw)
            for audio in (False,True):
                source=root/f'可变素材-{audio}.mp4'
                cmd=[str(FFMPEG),'-v','error','-y','-f','lavfi','-i','testsrc2=size=160x288:rate=30:duration=4']
                if audio:cmd+=['-f','lavfi','-i','sine=frequency=750:sample_rate=48000:duration=6']
                cmd+=['-vf',"setpts='if(lt(N,60),N/(15*TB),(4+(N-60)/30)/TB)'",'-fps_mode','vfr','-c:v','libx264','-preset','ultrafast']
                if audio:cmd+=['-c:a','aac']
                cmd+=[str(source)]
                subprocess.run(cmd,check=True,capture_output=True)
                before=digest(source);row=inspector.inspect(source)
                self.assertEqual(row['frame_rate_mode'],'VFR')
                out=normalize(row,inspector,root/'cache',FFMPEG)
                self.assertEqual(out['frame_rate_mode'],'CFR')
                self.assertEqual(float(out['frame_rate']),30)
                self.assertEqual(out['has_audio'],audio)
                self.assertNotEqual(out['path'],str(source))
                self.assertEqual(digest(source),before)
                self.assertLessEqual(abs(out['video_duration_us']-row['video_duration_us']),100000)
                self.assertFalse(out['normalization']['cache_hit'])
                again=normalize(row,inspector,root/'cache',FFMPEG)
                self.assertTrue(again['normalization']['cache_hit'])
                self.assertEqual(again['sha256'],out['sha256'])
                source.write_bytes(source.read_bytes()+b'changed')
                with self.assertRaisesRegex(ValueError,'已变化'):
                    normalize(row,inspector,root/'cache',FFMPEG)
