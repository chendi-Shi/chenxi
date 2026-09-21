from __future__ import annotations

import base64
import ctypes
import getpass
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .models import StrictModel
from .reporting import atomic_write


class DailyConfig(StrictModel):
    recipient: str
    sender: str
    model: str = "qwen-plus"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    free_quota_only_confirmed: bool = False
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = Field(default=465, ge=1, le=65535)
    timezone: str = "Asia/Shanghai"
    send_hour: int = Field(default=9, ge=0, le=23)
    lookback_hours: int = Field(default=36, ge=1, le=48)
    articles_per_company: int = Field(default=8, ge=1, le=12)
    data_dir: Path = Path("data/daily")

    @model_validator(mode="after")
    def valid(self):
        for address in (self.recipient, self.sender):
            if not re.fullmatch(r"[A-Za-z0-9_.+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", address):
                raise ValueError("invalid_email_address")
        parts = urlsplit(self.base_url)
        host = parts.hostname or ""
        if (
            parts.scheme != "https"
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.port not in {None, 443}
            or not (
                host == "dashscope.aliyuncs.com" or host.endswith(".cn-beijing.maas.aliyuncs.com")
            )
            or parts.path.rstrip("/") != "/compatible-mode/v1"
        ):
            raise ValueError("use_beijing_bailian_endpoint")
        if not self.model.strip():
            raise ValueError("model_required")
        return self


def load_config(path: Path) -> DailyConfig:
    config = DailyConfig.model_validate_json(path.read_text(encoding="utf-8-sig"))
    if not config.data_dir.is_absolute():
        config.data_dir = (path.resolve().parent / config.data_dir).resolve()
    return config


def protect(raw: bytes, decrypt: bool = False) -> bytes:
    """Windows DPAPI, current-user scope; plaintext secrets never touch disk."""
    if os.name != "nt":
        raise ValueError("use_environment_credentials_on_non_windows")

    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.windll.crypt32
    method = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    if not method(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ValueError("credential_encryption_failed")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


def credentials(config: DailyConfig) -> dict[str, str]:
    result = {
        "api_key": os.environ.get("DASHSCOPE_API_KEY", ""),
        "smtp_password": os.environ.get("SMTP_PASSWORD", ""),
    }
    path = config.data_dir / "credentials.dpapi"
    if not all(result.values()) and path.exists():
        saved = json.loads(protect(base64.b64decode(path.read_bytes()), decrypt=True))
        result = {key: value or saved.get(key, "") for key, value in result.items()}
    return result


def configure(path: Path):
    config = load_config(path)
    print("本机配置：密钥不回显，使用 Windows 当前用户加密保存。")
    print("在百炼北京控制台确认模型有免费额度，并开启该模型的‘免费额度用完即停’。")
    model = input(f"模型名 [{config.model}]：").strip() or config.model
    confirmed = input("已确认以上设置？输入 YES：").strip() == "YES"
    if not confirmed:
        raise ValueError("free_quota_guard_not_confirmed")
    api_key = getpass.getpass("百炼通用 API Key（北京地域，不回显）：").strip()
    password = getpass.getpass("QQ 邮箱 SMTP 授权码（不是登录密码，不回显）：").strip()
    if not api_key or not password:
        raise ValueError("both_credentials_required")
    protected = protect(json.dumps({"api_key": api_key, "smtp_password": password}).encode())
    atomic_write(config.data_dir / "credentials.dpapi", base64.b64encode(protected).decode())
    config.model = model
    config.free_quota_only_confirmed = True
    atomic_write(path, config.model_dump_json(indent=2))
    return {"status": "configured", "next": "run check --live, then run --send"}
