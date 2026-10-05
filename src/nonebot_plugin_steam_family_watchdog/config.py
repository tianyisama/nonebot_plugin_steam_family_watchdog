"""NoneBot dotenv 配置及 Web 运行时覆盖，统一使用 Pydantic 校验。"""

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from ipaddress import ip_address, ip_network

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Config(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_default=True)

    steam_family_data_dir: Path = Path("data/steam_family_watchdog")
    steam_family_poll_seconds: int = Field(default=300, ge=60, le=86400)
    steam_family_jitter_seconds: int = Field(default=10, ge=0, le=300)
    steam_family_language: str = Field(default="schinese", min_length=1, max_length=32)
    steam_family_missing_confirmations: int = Field(default=2, ge=1, le=10)
    steam_family_member_aliases: dict[str, str] = Field(default_factory=dict)
    steam_family_push_groups: list[str] = Field(default_factory=list)
    steam_family_push_users: list[str] = Field(default_factory=list)
    steam_family_bot_id: str | None = None
    steam_family_send_images: bool = True
    steam_family_replay_history: bool = False
    steam_family_max_push_per_tick: int = Field(default=5, ge=1, le=50)
    steam_family_push_retry_seconds: int = Field(default=60, ge=10, le=3600)
    steam_family_web_enabled: bool = True
    steam_family_web_host: str = "0.0.0.0"
    steam_family_web_port: int = Field(default=11454, ge=1, le=65535)
    steam_family_web_token: str = Field(default="", repr=False)
    steam_family_web_public_url: str = ""
    steam_family_login_host: str = "0.0.0.0"
    steam_family_login_port: int = Field(default=11453, ge=1, le=65535)
    steam_family_login_public_url: str = ""
    steam_family_login_timeout_seconds: int = Field(default=600, ge=60, le=3600)
    steam_family_login_trusted_proxies: list[str] = Field(default_factory=lambda: ["127.0.0.1", "::1"])
    steam_family_auto_public_ip: bool = True
    steam_family_public_ip: str = ""

    @field_validator("steam_family_poll_seconds", "steam_family_jitter_seconds", "steam_family_missing_confirmations",
                     "steam_family_max_push_per_tick", "steam_family_push_retry_seconds", "steam_family_web_port",
                     "steam_family_login_port", "steam_family_login_timeout_seconds", mode="before")
    @classmethod
    def integer_only(cls, value):
        if isinstance(value, bool) or isinstance(value, float):
            raise ValueError("必须为整数")
        return value

    @field_validator("steam_family_push_groups", "steam_family_push_users", mode="before")
    @classmethod
    def validate_ids(cls, value):
        if not isinstance(value, list):
            raise ValueError("推送目标必须为 list")
        result = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (str, int)):
                raise ValueError("群号 / 用户号必须为正整数字符串")
            text = str(item).strip()
            if not text.isascii() or not text.isdigit() or not 0 < int(text) <= 2**63 - 1:
                raise ValueError("群号 / 用户号必须为正整数")
            text = str(int(text))
            if text not in result:
                result.append(text)
        return result

    @field_validator("steam_family_bot_id", mode="before")
    @classmethod
    def validate_bot_id(cls, value):
        if value is None or value == "":
            return None
        return cls.validate_ids([value])[0]

    @field_validator("steam_family_member_aliases")
    @classmethod
    def validate_aliases(cls, value):
        if any(not key.isascii() or not key.isdigit() or len(key) != 17 or not name.strip()
               or len(name) > 128 for key, name in value.items()):
            raise ValueError("成员别名需使用 17 位 SteamID64，名称为 1～128 字符")
        return value

    @field_validator("steam_family_web_token")
    @classmethod
    def validate_token(cls, value):
        if value and (len(value) < 32 or value.startswith("replace-")):
            raise ValueError("管理密钥至少 32 字符；留空则自动生成")
        return value

    @field_validator("steam_family_web_public_url", "steam_family_login_public_url")
    @classmethod
    def validate_public_url(cls, value):
        if not value:
            return ""
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("公网地址必须为 HTTP/HTTPS URL，且不能包含账号、密码、查询参数或片段")
        try:
            parsed.port
        except ValueError as error:
            raise ValueError("公网地址端口无效") from error
        return value.rstrip("/") + "/"

    @field_validator("steam_family_public_ip")
    @classmethod
    def validate_ip(cls, value):
        return str(ip_address(value)) if value else ""

    @field_validator("steam_family_login_public_url")
    @classmethod
    def secure_login_url(cls, value):
        if value and urlsplit(value).scheme != "https":
            raise ValueError("非本地 Steam 认证地址必须使用 HTTPS")
        return value

    @field_validator("steam_family_login_trusted_proxies")
    @classmethod
    def trusted_proxies(cls, values):
        return [str(ip_network(value, strict=False)) for value in values]

    @field_validator("steam_family_data_dir")
    @classmethod
    def resolve_dir(cls, value):
        return value.expanduser().resolve()


# Network bindings, secrets and data paths are startup-only settings.
EDITABLE_FIELDS = {
    "steam_family_poll_seconds", "steam_family_jitter_seconds", "steam_family_language",
    "steam_family_missing_confirmations", "steam_family_member_aliases", "steam_family_push_groups",
    "steam_family_push_users", "steam_family_bot_id", "steam_family_send_images", "steam_family_replay_history",
    "steam_family_max_push_per_tick", "steam_family_push_retry_seconds",
}


def public_config(config: Config) -> dict[str, Any]:
    return {key: value for key, value in config.model_dump(mode="json").items() if key in EDITABLE_FIELDS}
