"""Source launcher; all generated data stays in the project UserData directory."""
import os,sys
from pathlib import Path
root=Path(__file__).resolve().parent
os.chdir(root)
if not (root/'config.native.json').exists():
    raise SystemExit('先复制 config.example.json 为 config.native.json；见 docs/OPEN_SOURCE_SETUP.md')
os.environ.setdefault('JIANYING_AGENT_STATE_DIR',str(root/'UserData'))
os.environ.setdefault('FRAMO_VOICE_CACHE',str(root/'UserData/VoiceCache'))
from agent_core.desktop_workbench import main
main()
