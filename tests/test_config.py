import os
import subprocess
import sys

import pytest

from audit_core.config import SettingsError, load_settings


def test_load_settings_requires_app_env() -> None:
    with pytest.raises(SettingsError, match="Missing required runtime setting: APP_ENV"):
        load_settings({})


def test_load_settings_accepts_explicit_environment() -> None:
    settings = load_settings({"APP_ENV": "development"})

    assert settings.environment == "development"
    assert settings.service_name == "verigence-audit-core"


def test_service_startup_fails_fast_without_required_setting() -> None:
    env = os.environ.copy()
    env.pop("APP_ENV", None)

    result = subprocess.run(
        [sys.executable, "-c", "import audit_core.main"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Missing required runtime setting: APP_ENV" in result.stderr
