from pathlib import Path

import pytest

from personal_ai_os.config import load_config


def test_offline_defaults_and_real_key_requirement():
    config = load_config(environ={})
    assert config.deepseek_model_id == "deepseek-flash"
    assert config.timezone == "Australia/Sydney"
    assert config.database_path == Path(".local/personal_ai_os.sqlite3")
    assert config.deepseek_api_key is None
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        load_config(require_model=True, environ={})
    assert load_config(require_model=True, environ={"DEEPSEEK_API_KEY": "test-key"}).deepseek_api_key == "test-key"


@pytest.mark.parametrize("model_id", ["gpt-4o", "gemini-2.0-flash", "deepseek-chat", ""])
def test_only_current_deepseek_models_are_allowed(model_id):
    with pytest.raises(ValueError, match="DeepSeek"):
        load_config(environ={"DEEPSEEK_MODEL_ID": model_id})


def test_invalid_timezone_fails_early():
    with pytest.raises(ValueError, match="APP_TIMEZONE"):
        load_config(environ={"APP_TIMEZONE": "Not/A_Zone"})
