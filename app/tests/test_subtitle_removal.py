import hashlib
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from PIL import Image
from agent_core.subtitle_removal import (LamaInpainter, RemovalCancelled,
    FastPainter, checked_region, detect_subtitle_regions, export_repaired,
    inspect_video, make_subtitle_tracker, read_frame, stabilize_repair)

TOOLS={'ffmpeg':'D:/JianyingAgentWork/Toolchain/ffmpeg.exe',
       'ffprobe':'D:/JianyingAgentWork/Toolchain/ffprobe.exe'}


class IdentityPainter:
    def load(self):pass
    def repair(self,image,region):return image


class SubtitleRemovalTests(unittest.TestCase):
    def test_automatic_detection_finds_controlled_chinese_subtitle(self):
        image=Image.open('D:/JianyingAgentWork/RepairTests/20261016/subtitle-removal/fixture-before.png').convert('RGB')
        regions=detect_subtitle_regions([image])
        self.assertTrue(any(r[0] <= 61 and r[1] <= 520 and r[2] >= 297 and r[3] >= 549 for r in regions), regions)

    def test_fast_painter_and_tracker_keep_regions_inside_frame(self):
        image=Image.new('RGB',(320,240),(180,180,180))
        tracker=make_subtitle_tracker((40,160,280,200),320,240)
        self.assertEqual(tracker(image),(40,160,280,200))
        result=FastPainter().repair(image,(40,160,280,200))
        self.assertEqual(result.size,image.size)

    def test_temporal_stabilizer_preserves_frame_outside_mask(self):
        previous=Image.new('RGB',(64,48),(20,30,40))
        current=Image.new('RGB',(64,48),(20,30,40))
        repaired=Image.new('RGB',(64,48),(120,130,140))
        result=stabilize_repair(previous,previous,current,repaired,(16,12,48,30))
        self.assertEqual(result.getpixel((0,0)),(120,130,140))
        self.assertNotEqual(result.getpixel((32,20)),(120,130,140))

    def test_mask_bounds_and_size_are_explicit(self):
        self.assertEqual(checked_region((10,60,90,80),100,100),(10,60,90,80))
        for region in ((-1,60,90,80),(10,60,110,80),(10,60,10,80),(0,0,100,100),None):
            with self.subTest(region=region),self.assertRaises(ValueError):
                checked_region(region,100,100)

    def test_repair_never_changes_pixels_outside_mask(self):
        import numpy as np
        class Session:
            def run(self,outputs,inputs):
                self_inputs.update(inputs)
                return [np.ones((1,3,512,512),dtype=np.float32)*32]
        self_inputs={}
        painter=LamaInpainter();painter.session=Session()
        original=Image.new('RGB',(320,240),(190,160,150))
        result=painter.repair(original,(80,170,240,200))
        before,after=np.asarray(original),np.asarray(result)
        self.assertTrue(np.array_equal(before[:170],after[:170]))
        self.assertTrue(np.array_equal(before[200:],after[200:]))
        self.assertTrue(np.array_equal(before[:,:80],after[:,:80]))
        self.assertEqual(result.getpixel((160,185)),(32,32,32))
        self.assertEqual(self_inputs['image'].shape,(1,3,512,512))
        self.assertEqual(self_inputs['mask'].shape,(1,1,512,512))
        self.assertTrue(set(np.unique(self_inputs['mask'])).issubset({0,1}))

    def test_model_integrity_is_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            model=Path(tmp)/'model.onnx';model.write_bytes(b'bad model')
            with self.assertRaisesRegex(ValueError,'校验失败'):
                LamaInpainter(model).load()


@unittest.skipUnless(all(Path(p).is_file() for p in TOOLS.values()),'Local FFmpeg required')
class SubtitleRemovalMediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.source=self.root/'原片.mp4'
        proc=subprocess.run([TOOLS['ffmpeg'],'-v','error','-nostdin',
            '-f','lavfi','-i','testsrc2=s=160x240:r=30000/1001',
            '-f','lavfi','-i','sine=frequency=620:sample_rate=48000',
            '-t','0.6','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac',str(self.source)],
            capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertEqual(proc.returncode,0,proc.stderr.decode(errors='replace'))
        self.info=inspect_video(self.source,TOOLS)
        self.original_hash=hashlib.sha256(self.source.read_bytes()).hexdigest()

    def tearDown(self):self.tmp.cleanup()

    def test_actual_mp4_retains_audio_duration_dimensions_and_fractional_fps(self):
        events=[];dest=self.root/'去字幕.mp4'
        report=export_repaired(self.info,(15,170,145,195),dest,TOOLS,
                               IdentityPainter(),progress=events.append)
        self.assertEqual(report['status'],'COMPLETED')
        output=inspect_video(dest,TOOLS)
        self.assertEqual((output['width'],output['height']),(160,240))
        self.assertEqual(output['fps'],'30000/1001')
        self.assertTrue(output['has_audio'])
        self.assertAlmostEqual(output['duration_s'],self.info['duration_s'],delta=.05)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),self.original_hash)
        self.assertEqual(read_frame(output,.1,TOOLS).size,(160,240))
        self.assertEqual(events[-1]['percent'],100)
        self.assertFalse(list(self.root.glob('.framo-removal-*')))

    def test_cancel_during_real_render_never_publishes_or_leaves_temp_mp4(self):
        cancel=threading.Event()
        class CancelPainter(IdentityPainter):
            def repair(self,image,region):
                cancel.set();return image
        dest=self.root/'cancel.mp4'
        with self.assertRaises(RemovalCancelled):
            export_repaired(self.info,(15,170,145,195),dest,TOOLS,CancelPainter(),cancel=cancel)
        self.assertFalse(dest.exists())
        self.assertFalse(list(self.root.glob('.framo-removal-*')))
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),self.original_hash)

    def test_existing_output_and_original_cannot_be_overwritten(self):
        for dest in (self.source,self.root/'exists.mp4'):
            if dest != self.source:dest.write_bytes(b'keep existing output')
            before=dest.read_bytes()
            with self.assertRaises(ValueError):
                export_repaired(self.info,(15,170,145,195),dest,TOOLS,IdentityPainter())
            self.assertEqual(dest.read_bytes(),before)

    def test_rotated_source_exports_in_display_orientation(self):
        rotated=self.root/'rotated.mp4'
        subprocess.run([TOOLS['ffmpeg'],'-v','error','-nostdin','-display_rotation','90',
            '-i',str(self.source),'-c','copy',str(rotated)],check=True,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        info=inspect_video(rotated,TOOLS)
        self.assertEqual((info['width'],info['height']),(240,160))
        self.assertEqual(read_frame(info,.1,TOOLS).size,(240,160))
        report=export_repaired(info,(20,110,220,135),self.root/'rotated-fixed.mp4',TOOLS,IdentityPainter())
        self.assertEqual((report['media']['width'],report['media']['height']),(240,160))


if __name__=='__main__':unittest.main()
