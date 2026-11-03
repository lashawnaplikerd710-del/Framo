"""Opt-in installed distribution paths; legacy owner installations are unchanged."""
import json
import os
from pathlib import Path


def is_installed(root):
    path = Path(root) / 'installed-profile.json'
    if not path.is_file():
        return False
    return json.loads(path.read_text(encoding='utf-8')).get('schema') == 'framo-installed-v1'


def prepare(root):
    root = Path(root).resolve()
    if not is_installed(root):
        return
    state = root / 'UserData'
    temporary = state / 'Temp'
    temporary.mkdir(parents=True, exist_ok=True)
    # The installer asks for a writable location. All substantial runtime data
    # follows that location, including model/speech caches and temporary media.
    os.environ.update(JIANYING_AGENT_STATE_DIR=str(state),
                      FRAMO_VOICE_CACHE=str(state / 'VoiceCache'),
                      TEMP=str(temporary), TMP=str(temporary),
                      HF_HOME=str(state / 'ModelCache'),
                      XDG_CACHE_HOME=str(state / 'ModelCache'),
                      PYTHONDONTWRITEBYTECODE='1')
