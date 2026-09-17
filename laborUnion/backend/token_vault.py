"""平台原生的登录令牌保护。

Windows 使用当前用户作用域的 DPAPI；Android 通过 Chaquopy 调用 Kotlin
``TokenVault``，由 Android Keystore 中的 AES-GCM 密钥完成加解密。调用方只把
返回的密文写入 settings.json，不保存密码或明文 Token。
"""
from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes


class TokenVaultError(RuntimeError):
    """本机安全存储不可用或密文无法解密。"""


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    value = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return value, buffer


def _dpapi_transform(data: bytes, entropy: bytes, *, decrypt: bool) -> bytes:
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError) as error:
        raise TokenVaultError("当前系统不支持 Windows DPAPI") from error

    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(entropy)
    output_blob = _DataBlob()
    _ = input_buffer, entropy_buffer  # 保持缓冲区存活到系统调用结束

    if decrypt:
        function = crypt32.CryptUnprotectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        description = ctypes.c_wchar_p()
        args = (
            ctypes.byref(input_blob),
            ctypes.byref(description),
            ctypes.byref(entropy_blob),
            None,
            None,
            0x1,  # CRYPTPROTECT_UI_FORBIDDEN
            ctypes.byref(output_blob),
        )
    else:
        function = crypt32.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.c_wchar_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        args = (
            ctypes.byref(input_blob),
            "LucimaTools login token",
            ctypes.byref(entropy_blob),
            None,
            None,
            0x1,
            ctypes.byref(output_blob),
        )

    function.restype = wintypes.BOOL
    if not function(*args):
        code = ctypes.get_last_error()
        raise TokenVaultError(f"Windows DPAPI 操作失败 ({code})")

    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if output_blob.pbData:
            kernel32.LocalFree(ctypes.cast(output_blob.pbData, ctypes.c_void_p))
        if decrypt and description:
            kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))


def _android_vault():
    try:
        from java import jclass  # type: ignore[import-not-found]

        return jclass("com.openrubi.lucimatools.TokenVault")
    except Exception as error:
        raise TokenVaultError("Android Keystore 桥不可用") from error


def protect(platform: str, plaintext: str, account: str) -> str:
    """使用当前平台安全存储加密文本，返回带版本前缀的密文。"""
    if not plaintext:
        raise TokenVaultError("不能保存空登录令牌")
    if platform == "android":
        try:
            return str(_android_vault().encrypt(plaintext, account))
        except TokenVaultError:
            raise
        except Exception as error:
            raise TokenVaultError("Android Keystore 加密失败") from error

    protected = _dpapi_transform(
        plaintext.encode("utf-8"),
        f"LucimaTools:{account}".encode("utf-8"),
        decrypt=False,
    )
    return "dpapi-v1:" + base64.b64encode(protected).decode("ascii")


def unprotect(platform: str, ciphertext: str, account: str) -> str:
    """解密 :func:`protect` 返回的密文。"""
    if platform == "android":
        try:
            return str(_android_vault().decrypt(ciphertext, account))
        except TokenVaultError:
            raise
        except Exception as error:
            raise TokenVaultError("Android Keystore 解密失败") from error

    prefix = "dpapi-v1:"
    if not ciphertext.startswith(prefix):
        raise TokenVaultError("不支持的 Windows Token 密文版本")
    try:
        protected = base64.b64decode(ciphertext[len(prefix):], validate=True)
        plaintext = _dpapi_transform(
            protected,
            f"LucimaTools:{account}".encode("utf-8"),
            decrypt=True,
        )
        return plaintext.decode("utf-8")
    except TokenVaultError:
        raise
    except Exception as error:
        raise TokenVaultError("Windows Token 密文已损坏") from error
