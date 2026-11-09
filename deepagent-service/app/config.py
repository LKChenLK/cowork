"""集中設定。one.properties(ONE_PROPERTIES_PATH,預設為 CWD 下的 one-local.properties——本機在
deepagent-service/ 目錄啟動即自動吃 repo 內 gitignored 的本機真實設定檔 one-local.properties
(進版控的 one.properties 是全 key 範本、secrets 留空,複製為 one-local.properties 後填值);
internal 環境掛載的是 one.properties 檔名的實值版,MUST 顯式設 ONE_PROPERTIES_PATH 指向掛載路徑)
存在時作為基底層,env var 逐欄位覆寫;不存在時只讀 env——優先序 env > properties 檔 > 欄位預設。"""

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

_DEFAULT_PROPERTIES_PATH = "one-local.properties"


def _properties_path() -> Path:
    return Path(os.environ.get("ONE_PROPERTIES_PATH", _DEFAULT_PROPERTIES_PATH))


def _parse_properties(properties_file: Path) -> dict[str, str]:
    """Java 式 KEY=value:空行與 # 註解跳過、首個 = 切分並 strip。
    無 = 的非空行是配置錯誤——啟動即失敗,NEVER 靜默跳過。"""
    parsed: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        properties_file.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError(
                f"one.properties line {line_number} missing '=' separator: {raw_line!r} ({properties_file})"
            )
        key, _, value = line.partition("=")
        parsed[key.strip()] = value.strip()
    return parsed


class PropertiesFileSource(PydanticBaseSettingsSource):
    def __init__(self, settings_cls: type[BaseSettings], properties_file: Path):
        super().__init__(settings_cls)
        self._values = _parse_properties(properties_file)

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return self._values.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        return {
            field_name: self._values[field_name]
            for field_name in self.settings_cls.model_fields
            if field_name in self._values
        }


class Settings(BaseSettings):
    model_config = SettingsConfigDict(case_sensitive=True)

    # 打 /chat、/repair 進來的固定 bearer(Java 端 ERD_AGENT_ANALYSIS_BEARER_TOKEN 鏡射同一個值);
    # 空字串在啟動時直接炸——NEVER 靜默放行未驗證請求。
    AGENT_API_BEARER_TOKEN: str = ""
    AGENT_AUTH_MODE: str = "bearer"
    AGENT_TOKEN_EXCHANGE_URL: str = ""
    AGENT_TOKEN_HEADER: str = ""
    AGENT_TOKEN_TTL: int = 300
    AGENT_SERVICE_ACCOUNT_KEY: str | None = None
    AGENT_SERVICE_ACCOUNT_KEY_FILE: str | None = None
    REPAIR_MODEL_CALL_TIMEOUT_SECONDS: float = 180.0
    AGENT_RUNTIME: str = "deepagents"
    AGENT_RECURSION_LIMIT: int = 80
    AGENT_MAX_TOKENS: int = 32768
    AGENT_REASONING_MAX_TOKENS: int = 8192
    AGENT_PROVIDER_SORT: str = ""
    AGENT_PROVIDER_IGNORE: str = ""
    AGENT_PROVIDER_REQUIRE_PARAMETERS: str = "true"
    AGENT_MODEL: str = "deepseek-v4-flash"
    OPENAI_BASE_URL: str | None = None
    OPENAI_API_KEY: str = "unused"
    AGENT_WORKSPACE_ROOT: str = "/data/workspace"
    AGENT_BUILTIN_SKILLS_DIR: str | None = None
    STORAGE_BACKEND: str = "local"
    S3_ENDPOINT: str = ""
    S3_BUCKET: str = "erd-cowork"
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_KEY_PREFIX: str = ""
    LANGFUSE_PUBLIC_KEY: str | None = None
    LANGFUSE_SECRET_KEY: str | None = None
    LANGFUSE_HOST: str | None = None

    # SSO header 名稱——入站(main.py `/chat`、`/repair` 讀取)與出站(mcp_adapter.py 轉送給
    # MCP server)用同一組名稱。名稱固定不變,可配置只為避免 internal header 名進版控;
    # 值一律走 header,NEVER 走 JSON body。
    SSO_TOKEN_HEADER: str = "X-SSO-Token"
    SSO_URL_HEADER: str = "X-SSO-Url"

    # connector tools 每 turn 呼叫上限(全部 connector tools 共用一個計數器)。
    CONNECTOR_CALL_BUDGET: int = 12

    # token key → service token 的 JSON dict(字串形式);key 由 catalog 各 connector entry
    # 明文宣告的 bearerTokenKey 決定(不是 connectorId)——多個 connector 可共用同一個
    # key(共用 gateway token 的場景)。空=全部 connector 不需認證。
    # 刻意宣告 str 不是 dict——PropertiesFileSource 對複雜型別沒有 env source 那種 JSON
    # 預解碼,宣告 dict 會在 properties 路徑炸 validation;見 connector_bearer_token()。
    CONNECTOR_BEARER_TOKENS: str = ""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        properties_file = _properties_path()
        if properties_file.exists():
            return (
                init_settings,
                env_settings,
                PropertiesFileSource(settings_cls, properties_file),
            )
        return (init_settings, env_settings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


class SecretResolutionError(Exception):
    """CONNECTOR_BEARER_TOKENS 配置不合法——訊息 NEVER 含任何 token 值。"""


def connector_bearer_token(token_key: str) -> str | None:
    raw_mapping = get_settings().CONNECTOR_BEARER_TOKENS
    if not raw_mapping:
        return None
    try:
        mapping = json.loads(raw_mapping)
    except json.JSONDecodeError as decode_error:
        raise SecretResolutionError("CONNECTOR_BEARER_TOKENS is not valid JSON") from decode_error
    if not isinstance(mapping, dict):
        raise SecretResolutionError("CONNECTOR_BEARER_TOKENS must be a JSON dict")
    token_value = mapping.get(token_key)
    if token_value is None:
        return None
    if not isinstance(token_value, str):
        raise SecretResolutionError("CONNECTOR_BEARER_TOKENS token value must be a string")
    return token_value or None
