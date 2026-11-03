"""Bundled author examples must never become a new template film's media."""
from pathlib import Path
import json
import tempfile
import unittest

from agent_core.motion_templates import TemplateLibrary, TemplateRunner


class TemplateSourceTests(unittest.TestCase):
    def test_author_demo_and_its_copy_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            demos = root/'skills/adu-motion-video/assets/demos'
            demos.mkdir(parents=True)
            demo = demos/'01-A.mp4'
            demo.write_bytes(b'author demo video')
            copy = root/'copied.mp4'
            copy.write_bytes(demo.read_bytes())
            original = root/'this-episode.mp4'
            original.write_bytes(b'user video')
            runner = TemplateRunner(TemplateLibrary(root/'state', root))
            for path in (demo, copy, demos):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, '作者示例'):
                    runner.reject_author_example(path)
            self.assertEqual(runner.reject_author_example(original), original)

    def test_ready_job_cannot_render_an_author_demo(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            demo = root/'skills/adu-motion-video/assets/demos/02-A.mp4'
            demo.parent.mkdir(parents=True)
            demo.write_bytes(b'author demo video')
            library = TemplateLibrary(root/'state', root)
            job = library.state/'jobs'/'existing-job'
            (job/'plan').mkdir(parents=True)
            (job/'job.json').write_text(json.dumps({'state':'READY','source':str(demo)}))
            (job/'request.json').write_text(json.dumps({'media':[]}))
            (job/'plan/report.json').write_text(json.dumps({'ready':True}))
            with self.assertRaisesRegex(ValueError, '作者示例'):
                TemplateRunner(library).render(job)
            self.assertEqual(json.loads((job/'job.json').read_text(encoding='utf-8'))['state'], 'FAILED')


if __name__ == '__main__':
    unittest.main()
