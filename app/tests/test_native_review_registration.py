import json
from pathlib import Path
import tempfile
import unittest
import shutil
import subprocess
import sys
from unittest.mock import patch

from agent_core.native_review_registration import (rewrite_paths, absolute_paths, file_manifest,
    validate_registered, commit_review_intent, RegistrationFailure, copy_checked,verify_dependency_copy,
    checkpoint_source_mappings, verify_manifest)
from agent_core.file_io import io_path
from agent_core.native_packaging import native_modules
from agent_core.task_store import atomic_json,digest


class NativeReviewRegistrationTests(unittest.TestCase):
    def test_checkpoint_video_alias_requires_original_source_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);alias=root/'base/media/video.mp4';alias.parent.mkdir(parents=True)
            alias.write_bytes(b'original source')
            snap={'materials':{'videos':[{'path':str(alias),'media_path':str(alias)}]}}
            targets={digest(alias):'D:/draft/media/source.mp4'}
            mapped=rewrite_paths(snap,checkpoint_source_mappings(snap,root,targets))
            self.assertEqual(mapped['materials']['videos'][0]['path'],targets[digest(alias)])
            alias.write_bytes(b'changed source')
            with self.assertRaisesRegex(RegistrationFailure,'source_hash_unknown'):
                checkpoint_source_mappings(snap,root,targets)

    def test_index_lock_recovers_after_process_death_and_rejects_live_writer(self):
        native_modules()
        from native_register import index_lock, append_index
        with tempfile.TemporaryDirectory() as tmp:
            index=Path(tmp)/'index.json'
            # A leftover file from an interrupted older implementation is inert.
            index.with_name(index.name+'.codex-registration.lock').write_text('old pid')
            scripts=Path(__file__).resolve().parents[1]/'toolchain/scripts'
            code=('import sys; from pathlib import Path; '
                  'sys.path.insert(0,sys.argv[1]); from native_register import index_lock; '
                  'lock=index_lock(Path(sys.argv[2])); lock.__enter__(); '
                  'print("locked",flush=True); sys.stdin.read()')
            child=subprocess.Popen([sys.executable,'-c',code,str(scripts),str(index)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                self.assertEqual(child.stdout.readline().strip(),'locked')
                with self.assertRaisesRegex(RuntimeError,'registration is active'):
                    with index_lock(index):pass
                child.terminate();child.wait(timeout=10)
                with patch('native_register.editor_pids',return_value=[]):
                    result=append_index(index,{'draft_id':'new','draft_fold_path':str(Path(tmp)/'draft')},Path(tmp))
                self.assertTrue(result['index_readback_equal'])
                self.assertEqual(result['new_entry_count'],1)
            finally:
                if child.poll() is None:child.terminate();child.wait(timeout=10)
                child.stdin.close();child.stdout.close();child.stderr.close()

    def test_rich_text_and_exact_path_prefix_are_rebased(self):
        old='C:/old';new='D:/new'
        value={'path':'C:\\old\\video.mp4','other':'C:/older/keep.mp4',
               'content':json.dumps({'styles':[{'font':{'path':'C:/old/fonts/a.ttf'}}]})}
        changed=rewrite_paths(value,[(old,new)])
        self.assertEqual(changed['path'],'D:/new/video.mp4')
        self.assertEqual(changed['other'],'C:/older/keep.mp4')
        self.assertIn('D:/new/fonts/a.ttf',absolute_paths(changed))
        native_modules()
        from native_register import rebase
        rebased=rebase(value,Path(old),Path(new),'old_id','new_id')
        self.assertEqual(json.loads(rebased['content'])['styles'][0]['font']['path'],'D:\\new\\fonts\\a.ttf')

    def fixture(self,root,registered=False):
        dest=root/'draft';dest.mkdir();(dest/'draft_content.json').write_bytes(b'native fixture')
        media=dest/'media';media.mkdir();(media/'source.mp4').write_bytes(b'media fixture')
        frame=root/'frame.png';frame.write_bytes(b'frame fixture')
        preview=root/'preview.json';atomic_json(preview,{'frames':[{'path':str(frame),'sha256':digest(frame)}]})
        index=root/'index.json'
        entry={'draft_id':'new','draft_fold_path':str(dest)}
        old={'draft_id':'old','draft_fold_path':str(root/'old'),'unknown':'preserve'}
        atomic_json(index,{'all_draft_store':([entry,old] if registered else [old]),'unknown_root':42})
        intent={'destination':str(dest),'destination_files':file_manifest(dest),'preview_report':str(preview),
            'preview_sha256':digest(preview),'index':str(index),'entry':entry,'index_before':{'all_draft_store':[old]},
            'validation':{'exact_native_frame_comparison':True},'source_packaging_report':'proof'}
        atomic_json(root/'registration-intent.json',intent)
        return {'intent_sha256':digest(root/'registration-intent.json')},dest,index

    def test_interruption_before_or_after_index_commit_never_duplicates_entry(self):
        native_modules()
        for registered in (False,True):
            with self.subTest(registered=registered), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);journal,dest,index=self.fixture(root,registered)
                with patch('native_register.editor_pids',return_value=[]):
                    first=commit_review_intent(root,journal)
                    second=commit_review_intent(root,journal)
                self.assertEqual(first['destination'],str(dest));self.assertTrue(second['local_review_only'])
                self.assertFalse(second['delivery_accepted'])
                saved=json.loads(index.read_text())
                self.assertEqual([r['draft_id'] for r in saved['all_draft_store']],['new','old'])
                self.assertEqual(saved['unknown_root'],42)
                self.assertEqual(validate_registered(root),second)

    def test_changed_dependency_or_conflicting_index_stops_without_overwrite(self):
        for change in ('media','index'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);journal,dest,index=self.fixture(root,True)
                if change=='media':(dest/'media/source.mp4').write_bytes(b'changed')
                else:
                    data=json.loads(index.read_text());data['all_draft_store'][0]['draft_id']='conflict';atomic_json(index,data)
                before=index.read_bytes()
                with self.assertRaises(RegistrationFailure):commit_review_intent(root,journal)
                self.assertEqual(index.read_bytes(),before)

    def test_resource_copy_preserves_long_files_and_empty_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source';target=root/'target'
            relative=Path('resources')/('long_a'*16)/('long_b'*16)/'render.rt'
            leaf=source/relative
            self.assertGreater(len(str(leaf)),260)
            io_path(leaf.parent).mkdir(parents=True);io_path(leaf).write_bytes(b'official resource')
            io_path(source/'resources/empty').mkdir()
            try:
                copy_checked(source,target)
                self.assertEqual(verify_dependency_copy(source,target),1)
                self.assertEqual(digest(leaf),digest(target/relative))
                self.assertTrue(io_path(target/'resources/empty').is_dir())
                self.assertEqual(len(file_manifest(target)),1)
                verify_manifest(target,file_manifest(source))
            finally:
                for path in (source,target):
                    self.assertTrue(path.is_relative_to(root))
                    if io_path(path).exists():shutil.rmtree(io_path(path))


if __name__=='__main__':unittest.main()
