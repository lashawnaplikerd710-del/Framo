import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
import shutil

from agent_core import native_resource_catalog as cat
from agent_core.file_io import io_path
from agent_core.native_packaging import verify_files


def reply(result):
    return subprocess.CompletedProcess([],0,json.dumps({'err_code':0,'result':result}).encode(),b'')


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.calls=[]
        self.client=SimpleNamespace(host=SimpleNamespace(install=self.root/'install'))

    def test_membership_and_unknown_free_status_never_download(self):
        for row in ({'is_vip':True}, {}, {'is_vip':False,'paid_mode':'subscribe'},
                    {'is_vip':False,'subscribe_package':'Pro'}):
            self.calls=[]
            def run(args,**kwargs):
                self.calls.append(args);return reply({'items':[{'resource_id':'123','source':1,**row}]})
            self.client.run=run
            with self.assertRaisesRegex(cat.ResourceFailure,'membership_or_free_status'):
                cat.acquire(self.client,'sticker','123',self.root/'out')
            self.assertEqual(len(self.calls),1);self.assertFalse((self.root/'out').exists())

    def test_query_identity_must_match_not_first_item(self):
        self.client.run=lambda *a,**k:reply({'items':[{'resource_id':'999','is_vip':False,'source':1}]})
        with self.assertRaisesRegex(cat.ResourceFailure,'identity_missing'):
            cat.acquire(self.client,'effect','123',self.root/'out')

    def test_download_files_hashed_and_never_claims_rendering_or_publication(self):
        out=self.root/'out'
        def run(args,**kwargs):
            self.calls.append(args)
            if args[1]=='query':return reply({'items':[{'resource_id':'123','is_vip':False,'source':1}]})
            payload=out/'payload/file.bin';payload.write_bytes(b'actual downloaded bytes')
            return reply({'resource_id':'123','path':str(payload)})
        self.client.run=run
        report=cat.acquire(self.client,'effect','123',out)
        self.assertEqual(len(report['files']),1)
        self.assertEqual(report['total_bytes'],23)
        self.assertFalse(report['native_application_verified']);self.assertFalse(report['delivery_accepted'])
        self.assertFalse(report['entitlement']['publishing_scope_verified'])
        self.assertEqual(len(self.calls),2)

    def test_external_download_path_rejected_and_failure_report_saved(self):
        external=self.root/'unrelated';external.write_bytes(b'existing')
        def run(args,**kwargs):
            if args[1]=='query':return reply({'items':[{'resource_id':'123','is_vip':False,'source':1}]})
            return reply({'resource_id':'123','path':str(external)})
        self.client.run=run
        with self.assertRaisesRegex(cat.ResourceFailure,'outside_resource'):
            cat.acquire(self.client,'effect','123',self.root/'out')
        self.assertEqual(external.read_bytes(),b'existing')
        report=json.loads((self.root/'out/provenance.json').read_text())
        self.assertEqual(report['status'],'failed')

    def test_music_needs_explicit_free_not_commerce_alone(self):
        self.assertFalse(cat.entitlement('music',{'is_commerce':True})['nonmember_metadata_verified'])
        self.assertFalse(cat.entitlement('music',{'paid_mode':'free','is_vip':True})['nonmember_metadata_verified'])
        self.assertTrue(cat.entitlement('music',{'paid_mode':'free','subscribe_package':''})['nonmember_metadata_verified'])

    def test_download_inventory_includes_files_beyond_windows_max_path(self):
        out=self.root/'out';package=out/'payload/123/package'
        leaf=package/('a'*100)/('b'*100)/'required.rt'
        self.assertGreater(len(str(leaf)),260)
        def run(args,**kwargs):
            if args[1]=='query':return reply({'items':[{'resource_id':'123','is_vip':False,'source':1}]})
            io_path(leaf.parent).mkdir(parents=True);io_path(leaf).write_bytes(b'native long-path dependency')
            return reply({'resource_id':'123','path':str(package)})
        self.client.run=run
        try:
            report=cat.acquire(self.client,'effect','123',out)
            self.assertEqual(len(report['files']),1)
            self.assertTrue(report['files'][0]['path'].endswith('required.rt'))
            self.assertEqual(report['file_inventory'],'extended_windows_paths_v1')
            verify_files({**report,'_directory':str(out)})
        finally:
            self.assertTrue(package.is_relative_to(self.root))
            if io_path(package).exists():shutil.rmtree(io_path(package))


if __name__=='__main__':unittest.main()
