import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from agent_core import internal_fonts as fonts, internal_resources as resources
from agent_core.resource_policy import ResourcePolicyBlocked


def fixture_font(license_text):
    values = [(1,'Fixture Family'),(13,license_text)]
    records=[];strings=b''
    for name,text in values:
        raw=text.encode('utf-16-be')
        records.append(struct.pack('>6H',3,1,0x409,name,len(raw),len(strings)))
        strings+=raw
    names=struct.pack('>3H',0,len(records),6+12*len(records))+b''.join(records)+strings
    return b'\x00\x01\x00\x00'+struct.pack('>4H',1,16,0,0)+b'name'+struct.pack('>3I',0,28,len(names))+names


class InternalResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def font_fixture(self, license_text='Apache License, Version 2.0'):
        path=self.root/'Resources/Font/SystemFont/test.ttf';path.parent.mkdir(parents=True)
        data=fixture_font(license_text);path.write_bytes(data)
        self.patch=patch.dict(fonts.FONTS,{'fixture':{'relative_path':path.relative_to(self.root).as_posix(),
            'sha256':hashlib.sha256(data).hexdigest(),'family':'Fixture Family',
            'license_marker':'Apache License, Version 2.0','license':'Apache-2.0','script_scope':'test only'}})
        self.patch.start();self.addCleanup(self.patch.stop)
        return path

    def test_verified_file_and_license_required_and_tampering_rejected(self):
        path=self.font_fixture()
        record=fonts.resolve_font(self.root,'fixture')
        self.assertEqual(record['origin'],'jianying_installation_builtin')
        self.assertFalse(record['member_required']);self.assertIsNone(record['online_resource_id'])
        path.write_bytes(path.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError,'changed'):fonts.resolve_font(self.root,'fixture')

    def test_matching_hash_without_license_is_insufficient(self):
        self.font_fixture('Commercial authorization required')
        with self.assertRaisesRegex(ValueError,'license'):fonts.resolve_font(self.root,'fixture')

    def test_arbitrary_paths_and_system_fonts_rejected(self):
        for value in ('C:/Windows/Fonts/msyhbd.ttc','../../font.ttf','yahei',[]):
            with self.assertRaises(ResourcePolicyBlocked):fonts.resolve_font(self.root,value)
        with self.assertRaises(ValueError):fonts.name_records(b'bad font')

    def test_glyph_coverage_rejects_fallback_for_bmp_and_non_bmp(self):
        format4 = (struct.pack('>7H',4,32,0,4,4,1,0) + struct.pack('>3H',65,65535,0) +
                   struct.pack('>2H',65,65535) + struct.pack('>2h',-64,1) + struct.pack('>2H',0,0))
        format12 = struct.pack('>HHIII',12,0,28,0,1)+struct.pack('>III',0x1F600,0x1F600,5)
        for mapping, present, absent in ((format4,'A','B'), (format12,'😀','A')):
            with self.subTest(present=present):
                cmap = struct.pack('>2H',0,1)+struct.pack('>HHI',3,10,12)+mapping
                data = b'\x00\x01\x00\x00'+struct.pack('>4H',1,16,0,0)+b'cmap'+struct.pack('>3I',0,28,len(cmap))+cmap
                path=self.root/'glyph-test.ttf';path.write_bytes(data)
                record={'path':str(path),'sha256':hashlib.sha256(data).hexdigest()}
                self.assertTrue(fonts.validate_text(record,present))
                with self.assertRaisesRegex(ValueError,'fallback refused'):fonts.validate_text(record,absent)

    def mock_cli(self, envelope):
        binary=self.root/'lyra-cli.exe';binary.write_bytes(b'fixture-executable-not-run')
        sha=hashlib.sha256(binary.read_bytes()).hexdigest()
        response=SimpleNamespace(returncode=0 if envelope.get('err_code')==0 else 1,
                                 stdout=json.dumps(envelope).encode(),stderr=b'private-token')
        return patch.object(resources,'LYRA_SHA256',sha),patch.object(resources.subprocess,'run',return_value=response)

    def test_auth_failure_preserved_and_model_keys_not_forwarded(self):
        pin,runner=self.mock_cli({'err_code':-100015,'err_msg':'private-token'})
        with pin,runner as call,patch.object(resources,'bridge_status',return_value={'status':'stale'}),\
                patch.dict('os.environ',{'DEEPSEEK_API_KEY':'private-key','UNRELATED_TOKEN':'private-token'}):
            report=resources.search(self.root,'sticker','宠物装饰')
        self.assertEqual(report['status'],'HOST_AUTH_REQUIRED')
        self.assertEqual(report['usable_resources'],[])
        self.assertNotIn('private',json.dumps(report))
        env=call.call_args.kwargs['env']
        self.assertNotIn('DEEPSEEK_API_KEY',env);self.assertNotIn('UNRELATED_TOKEN',env)
        self.assertNotIn('shell',call.call_args.kwargs)

    def test_search_metadata_cannot_claim_resource_applied_or_free(self):
        pin,runner=self.mock_cli({'err_code':0,'result':{'items':[{'resource_id':'123','title':'sample',
                    'is_vip':False,'token':'secret','download_url':'private'}]}})
        with pin,runner,patch.object(resources,'bridge_status',return_value={'status':'missing'}):
            report=resources.search(self.root,'music','轻快')
        self.assertEqual(report['status'],'metadata_received_unverified')
        self.assertFalse(report['native_application_verified']);self.assertFalse(report['download_performed'])
        self.assertEqual(report['usable_resources'],[])
        self.assertNotIn('secret',json.dumps(report));self.assertNotIn('private',json.dumps(report))

    def test_unknown_cli_and_invalid_arguments_never_start_process(self):
        (self.root/'lyra-cli.exe').write_bytes(b'not-the-tested-binary')
        with patch.object(resources.subprocess,'run') as call:
            with self.assertRaises(ValueError):resources.search(self.root,'music','valid')
            with self.assertRaises(ValueError):resources.search(self.root,'unknown','valid')
            with self.assertRaises(ValueError):resources.search(self.root,'music','bad\ntext')
            call.assert_not_called()
