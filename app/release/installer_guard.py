"""Installer-only guard: refuse replacement while this installation is active."""
import json
from pathlib import Path
import sys
import psutil

root = Path(__file__).resolve().parent
active = []
for proc in psutil.process_iter(['pid', 'exe']):
    try:
        exe = proc.info['exe']
        if proc.pid not in (psutil.Process().pid, psutil.Process().ppid()) and exe and Path(exe).resolve().is_relative_to(root):
            active.append(proc.pid)
    except (psutil.Error, OSError):
        pass
raise SystemExit(2 if active else 0)
