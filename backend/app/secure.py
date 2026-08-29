"""API Key 等敏感字段的透明加解密。

- 使用 cryptography.Fernet（对称加密）。
- 密钥存于项目内 `.agents/secret.key`（自动生成，权限受控）；不落库。
- 对外暴露 encrypt / decrypt：对非空字符串加解密；已加密/明文都能安全处理（幂等、容错）。
"""
from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken

# 项目根（工作区）= 后端目录上上级
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_ROOT = os.path.dirname(_BACKEND_DIR)
_SECRET_DIR = os.path.join(_PROJECT_ROOT, ".agents")
_SECRET_PATH = os.path.join(_SECRET_DIR, "secret.key")

_PREFIX = "enc$"  # 明文密文区分前缀（避免重复加密/误解密明文）

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is not None:
        return _fernet
    os.makedirs(_SECRET_DIR, exist_ok=True)
    if not os.path.exists(_SECRET_PATH):
        if not os.path.exists(_SECRET_PATH):
            key = Fernet.generate_key()
            with open(_SECRET_PATH, "wb") as f:
                f.write(key)
    with open(_SECRET_PATH, "rb") as f:
        _fernet = Fernet(f.read().strip())
    return _fernet


def is_encrypted(value: str) -> bool:
    """判断一个值是否已是密文。"""
    return bool(value) and value.startswith(_PREFIX)


def encrypt(plaintext: str) -> str:
    """加密明文；空串原样返回；已是密文则幂等返回。"""
    if not plaintext or is_encrypted(plaintext):
        return plaintext
    try:
        token = _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")
        return _PREFIX + token
    except Exception:
        # 加密失败（如缺 cryptography）时明文回落，保证系统可用
        return plaintext


def decrypt(cipher: str) -> str:
    """解密密文；明文/空/解密失败原样返回（容错，不中断流程）。"""
    if not cipher or not is_encrypted(cipher):
        return cipher
    token = cipher[len(_PREFIX):]
    try:
        return _get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
    except Exception:
        return cipher
