"""Private, subprocess-only adapter for the installed Jianying draft codec.

Adapted 2026-09-20 from aoguai/pyJianYingDraft, draft_crypto.py at
4a7730c9a14e91aa497e723c85b5c433a62a163c (Apache-2.0).
Original project copyright 2024 Gary Guan and its contributors.

Modifications: only the reviewed native ABI bridge is retained; all environment
discovery, generic file writers, CLI entry points and optional roundtrip paths
are removed. The public wrapper verifies the exact DLL and signature. No editor
DLL is distributed. This is a private ABI, not an official supported SDK.
"""

import ctypes
import os
from pathlib import Path
import sys


DECRYPT_SYMBOL = (
    "?decrypt@EncryptUtils@lvve@@QEAA?AV?$basic_string@DU?$char_traits@D@std@@"
    "V?$allocator@D@2@@std@@AEBV34@0AEA_N@Z"
)
ENCRYPT_SYMBOL = (
    "?encrypt@EncryptUtils@lvve@@QEAA?AV?$basic_string@DU?$char_traits@D@std@@"
    "V?$allocator@D@2@@std@@AEBV34@@Z"
)
ENABLE_SYMBOL = "?enable@EncryptUtils@lvve@@QEAAX_N@Z"


class _StringData(ctypes.Union):
    _fields_ = [("small", ctypes.c_char * 16), ("ptr", ctypes.c_void_p)]


class _String(ctypes.Structure):
    _fields_ = [("data", _StringData), ("size", ctypes.c_ulonglong),
                ("capacity", ctypes.c_ulonglong)]


class _Argument:
    def __init__(self, data):
        self.value = _String()
        self.value.size = len(data)
        self.buffer = None
        if len(data) < 16:
            self.value.capacity = 15
            if data:
                ctypes.memmove(ctypes.addressof(self.value.data), data, len(data))
        else:
            self.buffer = ctypes.create_string_buffer(data)
            self.value.capacity = len(data)
            self.value.data.ptr = ctypes.cast(self.buffer, ctypes.c_void_p).value


def _take(value):
    if value.size > 256 * 1024 * 1024:
        raise RuntimeError("Native codec returned an excessive output size")
    if value.capacity < 16:
        if value.size > 15:
            raise RuntimeError("Native codec returned an invalid small string")
        return ctypes.string_at(ctypes.addressof(value.data), value.size)
    if not value.data.ptr or value.size > value.capacity:
        raise RuntimeError("Native codec returned an invalid heap string")
    return ctypes.string_at(value.data.ptr, value.size)


class NativeDraftCodec:
    """Use for one operation in a disposable, validated worker process only."""

    def __init__(self, install_dir):
        if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise RuntimeError("Windows with 64-bit Python is required")
        folder = Path(install_dir).resolve(strict=True)
        self.cookie = os.add_dll_directory(str(folder))
        # Dependency search is limited to the installed DLL directory, Python's
        # application directory, registered DLL directories and System32.
        self.dll = ctypes.WinDLL(str(folder / "videoeditor.dll"),
                                 winmode=0x100 | 0x200 | 0x400 | 0x800,
                                 use_last_error=True)
        pointer = ctypes.POINTER(_String)
        self.decrypt_fn = self._bind(DECRYPT_SYMBOL, ctypes.WINFUNCTYPE(
            pointer, ctypes.c_void_p, pointer, pointer, pointer,
            ctypes.POINTER(ctypes.c_bool)))
        self.encrypt_fn = self._bind(ENCRYPT_SYMBOL, ctypes.WINFUNCTYPE(
            pointer, ctypes.c_void_p, pointer, pointer))
        self.enable_fn = self._bind(ENABLE_SYMBOL, ctypes.WINFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_bool))

    def _bind(self, name, prototype):
        address = ctypes.cast(self.dll[name], ctypes.c_void_p).value
        if not address:
            raise RuntimeError("Required native codec export is missing")
        return prototype(address)

    def decode(self, data):
        source, options, output = _Argument(data), _Argument(b"{}"), _String()
        ok = ctypes.c_bool(False)
        self.decrypt_fn(None, ctypes.byref(output), ctypes.byref(source.value),
                        ctypes.byref(options.value), ctypes.byref(ok))
        result = _take(output)
        if not ok.value or not result:
            raise RuntimeError("Native draft decode failed")
        return result

    def encode(self, data):
        self.enable_fn(None, True)
        source, output = _Argument(data), _String()
        self.encrypt_fn(None, ctypes.byref(output), ctypes.byref(source.value))
        result = _take(output)
        if not result or result == data:
            raise RuntimeError("Native draft encode did not produce encoded bytes")
        if self.decode(result) != data:
            raise RuntimeError("Native draft encode/decode byte roundtrip failed")
        return result
