import hashlib,json,sys,unittest
from pathlib import Path
from copy import deepcopy
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from adaptation import load_profile,validate_profile
from build_macro_project import bind_authored_block
from pack_layout import resolve_layout,install_layout
import tempfile
class PresenterPacks(unittest.TestCase):
 def test_new_versions_preserve_timing_media_sfx_and_all_text_bindings(self):
  rows=json.loads((ROOT/'references/presenter-motion-versions.json').read_text())
  for row in rows:
   parent=ROOT/'packs'/row['family'];old=json.loads((parent/row['from']/'manifest.json').read_text());p=parent/row['to'];new=json.loads((p/'manifest.json').read_text())
   self.assertEqual([s['id']for s in old['scenes']],[s['id']for s in new['scenes']])
   for a,b in zip(old['scenes'],new['scenes']):
    for key in ['source','cues','sfx','minFrames','maxHoldFrames','minFollowingFrames']:self.assertEqual(a.get(key),b.get(key),(row['family'],a['id'],key))
    if new.get('sourceFormat')=='authored-unit/1':
     values={s['id']:s['sourceText']for s in b.get('slots',[])if s['type']in['text','dynamicText']}
     contract=deepcopy(b)
     for s in contract.get('slots',[]):s['mustChange']=False
     bind_authored_block((p/b['sourceCodeFile']).read_text(),contract,values,b['id'])
   self.assertEqual((p/'audio_timeline.json').read_bytes(),(parent/row['from']/'audio_timeline.json').read_bytes())
   for name,digest in new['files'].items():self.assertEqual(hashlib.sha256((p/name).read_bytes()).hexdigest(),digest)
   validate_profile(load_profile(new,ROOT/'adaptation-profiles'),new)
   with tempfile.TemporaryDirectory()as tmp:
    stage=Path(tmp);(stage/'config.js').write_text('window.CONFIG={};')
    _,script,files=install_layout(p,new,{'layout':resolve_layout(new,{'layout':'portrait'})},stage)
    self.assertIn(new['layouts']['portrait']['runtime'],files)
   self.assertEqual(new['runtimeFiles'][0],'presenter_motion.js')
if __name__=='__main__':unittest.main()
