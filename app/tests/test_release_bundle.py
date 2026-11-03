import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
with patch.object(sys,'path',[str(ROOT/'release'),*sys.path]):
    spec=importlib.util.spec_from_file_location('framo_release_builder',ROOT/'release/build_installer.py')
    builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(builder)


class ReleaseBundleTests(unittest.TestCase):
    def test_public_docs_exclude_qa_jobs_and_private_artifact_paths(self):
        with tempfile.TemporaryDirectory() as raw:
            source=Path(raw)/'source';source.mkdir();target=Path(raw)/'public'
            for name in builder.PUBLIC_DOCS:
                (source/name).write_text('# Usage\nKeep this rule.\nLocal D:/Private/clip.mp4\n'
                    'job_'+'a'*32+'\n',encoding='utf-8')
            (source/'qa-owner.md').write_text('owner task history',encoding='utf-8')
            builder.copy_public_docs(source,target)
            self.assertEqual({p.name for p in target.iterdir()},builder.PUBLIC_DOCS)
            for p in target.iterdir():self.assertEqual(p.read_text(encoding='utf-8'),'# Usage\nKeep this rule.\n')

    def test_real_public_docs_have_no_private_job_or_artifact_references(self):
        with tempfile.TemporaryDirectory() as raw:
            target=Path(raw)/'public';builder.copy_public_docs(ROOT/'docs',target)
            for p in target.iterdir():
                content=p.read_text(encoding='utf-8')
                self.assertNotRegex(content,r'[A-Z]:[/\\]|job_[0-9a-f]{16,64}')
            self.assertIn('固定开头四十秒',(target/'long-video-no-voice.md').read_text(encoding='utf-8'))

    def test_payload_scanner_rejects_history_and_non_demo_video(self):
        for name in ('history.sqlite3','settings.json','user-footage.mp4'):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as raw:
                root=Path(raw);(root/name).write_bytes(b'private')
                with self.assertRaises(ValueError):builder.scan_and_manifest(root)


if __name__=='__main__':unittest.main()
