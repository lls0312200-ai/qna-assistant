"""Password hashing and local secret protection; no reversible password storage."""
from __future__ import annotations

import base64
import ctypes
import os
from pathlib import Path
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken

_PASSWORDS = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not 10 <= len(password) <= 1024:
        raise ValueError("비밀번호는 10~1,024자로 입력하세요.")
    return _PASSWORDS.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(password, str) or len(password) > 1024:
        return False
    try:
        return _PASSWORDS.verify(encoded, password)
    except (VerificationError, InvalidHashError, TypeError):
        return False


def _dpapi(data: bytes, protect: bool) -> bytes:
    """Windows current-user DPAPI. UI and machine-wide decryption are disabled."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, result = Blob(len(data), buffer), Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise OSError(ctypes.get_last_error(), "Windows DPAPI 키 보호에 실패했습니다.")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel.LocalFree(result.pbData)


def load_master_key(data_dir: Path) -> bytes:
    """DPAPI on Windows; chmod 0600 development key on other platforms."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    windows = os.name == "nt"
    path = data_dir / ("master-key.dpapi" if windows else "master-key.dev")
    if not path.exists():
        key = secrets.token_bytes(32)
        protected = _dpapi(key, True) if windows else key
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as target:
                target.write(protected)
                target.flush()
                os.fsync(target.fileno())
    protected = path.read_bytes()
    key = _dpapi(protected, False) if windows else protected
    if len(key) != 32:
        raise ValueError("저장된 암호화 키가 손상되었습니다. 기존 키를 복구해야 합니다.")
    if not windows:
        path.chmod(0o600)
    return key


def _fernet(key: bytes) -> Fernet:
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError("암호화 키는 32바이트여야 합니다.")
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(value: str, key: bytes) -> str:
    return _fernet(key).encrypt(value.encode("utf-8")).decode("ascii") if value else ""


def decrypt_secret(value: str, key: bytes) -> str:
    if not value:
        return ""
    try:
        return _fernet(key).decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError) as exc:
        raise ValueError("비밀값을 복호화할 수 없습니다. 원래 Windows 사용자와 보호 키가 필요합니다.") from exc
