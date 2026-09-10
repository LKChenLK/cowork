"""app/config.py 的 Settings 載入與 one.properties 層疊優先序語意（env > 檔案 > 預設）。"""

import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _write_properties(tmp_path, content: str):
    properties_file = tmp_path / "one.properties"
    properties_file.write_text(content, encoding="utf-8")
    return properties_file


def test_no_properties_file_reads_env(monkeypatch, tmp_path):
    monkeypatch.setenv("ONE_PROPERTIES_PATH", str(tmp_path / "absent.properties"))
    monkeypatch.setenv("AGENT_MODEL", "env-model")
    assert get_settings().AGENT_MODEL == "env-model"


def test_env_overrides_properties_file(monkeypatch, tmp_path):
    properties_file = _write_properties(tmp_path, "AGENT_MODEL=file-model\n")
    monkeypatch.setenv("ONE_PROPERTIES_PATH", str(properties_file))
    monkeypatch.setenv("AGENT_MODEL", "env-model")
    settings = get_settings()
    assert settings.AGENT_MODEL == "env-model"


def test_properties_file_fills_when_env_unset(monkeypatch, tmp_path):
    properties_file = _write_properties(tmp_path, "AGENT_MODEL=file-model\n")
    monkeypatch.setenv("ONE_PROPERTIES_PATH", str(properties_file))
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("AGENT_TOKEN_TTL", raising=False)
    settings = get_settings()
    # env 未設該欄位 → 落到檔案值
    assert settings.AGENT_MODEL == "file-model"
    # 檔案與 env 都沒設的 key → 落到欄位預設值
    assert settings.AGENT_TOKEN_TTL == 300


def test_properties_parsing_comments_blanks_and_equals_in_value(monkeypatch, tmp_path):
    properties_file = _write_properties(
        tmp_path,
        "# comment\n\nOPENAI_BASE_URL=https://host/v1?a=b=c\n  AGENT_TOKEN_TTL = 120 \n",
    )
    monkeypatch.setenv("ONE_PROPERTIES_PATH", str(properties_file))
    # env 對這兩個 key 不設值，避免殘留環境蓋掉檔案值（層疊優先序下 env 若有值會蓋過）
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("AGENT_TOKEN_TTL", raising=False)
    settings = get_settings()
    assert settings.OPENAI_BASE_URL == "https://host/v1?a=b=c"
    assert settings.AGENT_TOKEN_TTL == 120


def test_properties_bad_line_fails_loud(monkeypatch, tmp_path):
    properties_file = _write_properties(tmp_path, "AGENT_MODEL=ok\nthis-line-has-no-separator\n")
    monkeypatch.setenv("ONE_PROPERTIES_PATH", str(properties_file))
    with pytest.raises(RuntimeError, match="line 2"):
        get_settings()


def test_defaults_without_any_source(monkeypatch, tmp_path):
    monkeypatch.setenv("ONE_PROPERTIES_PATH", str(tmp_path / "absent.properties"))
    for key in ("AGENT_MODEL", "AGENT_TOKEN_TTL", "LANGFUSE_PUBLIC_KEY"):
        monkeypatch.delenv(key, raising=False)
    settings = get_settings()
    assert settings.AGENT_MODEL == "deepseek-v4-flash"
    assert settings.AGENT_TOKEN_TTL == 300
    assert settings.LANGFUSE_PUBLIC_KEY is None
