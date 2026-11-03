"""Extended-length Windows IO paths; persisted/native paths remain ordinary."""
import os
from pathlib import Path


def io_path(path):
    path=Path(path).absolute()
    value=str(path)
    if os.name=='nt' and not value.startswith('\\\\?\\'):
        value='\\\\?\\UNC\\'+value[2:] if value.startswith('\\\\') else '\\\\?\\'+value
    return Path(value)


def logical_path(path):
    value=str(path)
    if value.startswith('\\\\?\\UNC\\'):value='\\\\'+value[8:]
    elif value.startswith('\\\\?\\'):value=value[4:]
    return Path(value)
