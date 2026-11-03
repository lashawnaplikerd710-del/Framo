"""Portable offline regression entry; no provider calls or production state."""
from pathlib import Path
import os,sys,unittest
root=Path(__file__).resolve().parents[1]/'app'
os.chdir(root);sys.path.insert(0,str(root))
names=['test_locked_opening','test_original_montage','test_long_video',
       'test_editing_rules','test_batch_model','test_render_progress',
       'test_installed_runtime','test_provider']
suite=unittest.defaultTestLoader.loadTestsFromNames(['tests.'+n for n in names])
result=unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
