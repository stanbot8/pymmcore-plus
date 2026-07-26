from __future__ import annotations

import ctypes
import mmap
import msvcrt
import os
import struct
from ctypes import wintypes
from hashlib import sha256
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, cast

if TYPE_CHECKING:
    from io import TextIOWrapper
    from logging import LogRecord

_FILE_APPEND_DATA = 0x0004
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_ALL = 0x0001 | 0x0002 | 0x0004
_CREATE_ALWAYS = 2
_OPEN_ALWAYS = 4
_FILE_ATTRIBUTE_NORMAL = 0x0080
_WAIT_OBJECT_0 = 0
_WAIT_ABANDONED = 0x0080
_WAIT_FAILED = 0xFFFFFFFF
_INFINITE = 0xFFFFFFFF
_DRIVE_REMOTE = 4
_GENERATION = struct.Struct("<Q")

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_create_file = _kernel32.CreateFileW
_create_file.argtypes = (
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
)
_create_file.restype = wintypes.HANDLE
_create_mutex = _kernel32.CreateMutexW
_create_mutex.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
_create_mutex.restype = wintypes.HANDLE
_wait_for_single_object = _kernel32.WaitForSingleObject
_wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
_wait_for_single_object.restype = wintypes.DWORD
_release_mutex_handle = _kernel32.ReleaseMutex
_release_mutex_handle.argtypes = (wintypes.HANDLE,)
_release_mutex_handle.restype = wintypes.BOOL
_close_handle = _kernel32.CloseHandle
_close_handle.argtypes = (wintypes.HANDLE,)
_close_handle.restype = wintypes.BOOL
_get_drive_type = _kernel32.GetDriveTypeW
_get_drive_type.argtypes = (wintypes.LPCWSTR,)
_get_drive_type.restype = wintypes.UINT

_INVALID_HANDLE = wintypes.HANDLE(-1).value


def is_remote_path(path: str | Path) -> bool:
    """Return whether a path is on a Windows remote drive."""
    anchor = Path(path).resolve().anchor
    return not anchor or _get_drive_type(anchor) == _DRIVE_REMOTE


class WindowsRotatingFileHandler(RotatingFileHandler):
    """Rotate a local Windows log safely between processes."""

    def __init__(
        self,
        filename: str | Path,
        maxBytes: int = 0,
        backupCount: int = 0,
    ) -> None:
        super().__init__(
            filename,
            maxBytes=maxBytes,
            backupCount=backupCount,
            delay=True,
        )
        identity = os.path.normcase(os.path.abspath(self.baseFilename)).encode()
        shared_name = sha256(identity).hexdigest()
        self._mutex: int | None = int(
            _create_mutex(None, False, f"Global\\pymmcore-plus-log-{shared_name}")
        )
        if not self._mutex:
            raise ctypes.WinError(ctypes.get_last_error())

        log_path = Path(self.baseFilename)
        self._generation_path = log_path.with_name(f".__{log_path.name}.generation")
        self._generation_file: BinaryIO | None = None
        self._generation_map: mmap.mmap | None = None
        self._seen_generation = 0
        self._force_reopen = False
        try:
            self._open_generation()
        except BaseException:
            self._close_generation()
            _close_handle(self._mutex)
            self._mutex = None
            raise

    def _wait_for_mutex(self) -> bool:
        if self._mutex is None:
            raise RuntimeError("The Windows log mutex is closed.")
        result = int(_wait_for_single_object(self._mutex, _INFINITE))
        if result == _WAIT_FAILED:
            raise ctypes.WinError(ctypes.get_last_error())
        if result not in (_WAIT_OBJECT_0, _WAIT_ABANDONED):
            raise RuntimeError(f"Unexpected Windows mutex result: {result}")
        return result == _WAIT_ABANDONED

    def _release_mutex(self) -> None:
        if not _release_mutex_handle(self._mutex):
            raise ctypes.WinError(ctypes.get_last_error())

    def _open_generation(self) -> None:
        abandoned = self._wait_for_mutex()
        try:
            try:
                generation_file = open(self._generation_path, "r+b")
            except FileNotFoundError:
                generation_file = open(self._generation_path, "w+b")
            self._generation_file = generation_file
            if os.fstat(generation_file.fileno()).st_size != _GENERATION.size:
                generation_file.seek(0)
                generation_file.write(_GENERATION.pack(0))
                generation_file.truncate()
                generation_file.flush()
            self._generation_map = mmap.mmap(
                generation_file.fileno(), _GENERATION.size, access=mmap.ACCESS_WRITE
            )
            self._seen_generation = self._read_generation()
            if abandoned:
                self._advance_generation(update_seen=True)
        finally:
            self._release_mutex()

    def _read_generation(self) -> int:
        if self._generation_map is None:
            return self._seen_generation
        return cast(
            "int", _GENERATION.unpack(self._generation_map[: _GENERATION.size])[0]
        )

    def _advance_generation(self, *, update_seen: bool) -> None:
        if self._generation_map is None:
            return
        generation = (self._read_generation() + 1) % (1 << 64)
        self._generation_map.seek(0)
        self._generation_map.write(_GENERATION.pack(generation))
        self._generation_map.flush()
        if update_seen:
            self._seen_generation = generation

    def _check_stream(self) -> None:
        generation = self._read_generation()
        if self._force_reopen or generation != self._seen_generation:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            self._seen_generation = generation
            self._force_reopen = False

    def emit(self, record: LogRecord) -> None:
        locked = False
        try:
            abandoned = self._wait_for_mutex()
            locked = True
            if abandoned:
                self._advance_generation(update_seen=False)
                self._force_reopen = True
            self._check_stream()
            super().emit(record)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            self.handleError(record)
        finally:
            if locked:
                self._release_mutex()

    def _open(self) -> TextIOWrapper:
        if self.mode == "a":
            access = _FILE_APPEND_DATA
            disposition = _OPEN_ALWAYS
            descriptor_flags = os.O_APPEND | os.O_WRONLY | os.O_TEXT
        elif self.mode == "w":
            access = _GENERIC_WRITE
            disposition = _CREATE_ALWAYS
            descriptor_flags = os.O_WRONLY | os.O_TEXT
        else:
            return super()._open()

        handle = _create_file(
            self.baseFilename,
            access,
            _FILE_SHARE_ALL,
            None,
            disposition,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle == _INVALID_HANDLE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            descriptor = msvcrt.open_osfhandle(handle, descriptor_flags)
        except BaseException:
            _close_handle(handle)
            raise
        return cast(
            "TextIOWrapper",
            open(
                descriptor,
                mode=self.mode,
                encoding=self.encoding,
                errors=self.errors,
                closefd=True,
            ),
        )

    def doRollover(self) -> None:
        try:
            super().doRollover()
        finally:
            self._advance_generation(update_seen=True)

    def _close_generation(self) -> None:
        if self._generation_map is not None:
            self._generation_map.close()
            self._generation_map = None
        if self._generation_file is not None:
            self._generation_file.close()
            self._generation_file = None

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._close_generation()
            if self._mutex:
                _close_handle(self._mutex)
                self._mutex = None
