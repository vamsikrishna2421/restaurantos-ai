import pytest
from pydantic import SecretStr

from restaurantos.config import AIMode, Settings
from restaurantos.meta.spend import SpendGuard

FAKE_KEY = "test-key-not-real"


@pytest.fixture
def settings(tmp_path):
    return Settings(model_api_key=SecretStr(FAKE_KEY), data_dir=tmp_path, ai_mode=AIMode.LIVE,
                    daily_usd_cap=5.0, total_usd_cap=10.0, session_usd_cap=1.0)


@pytest.fixture
def guard(settings):
    return SpendGuard(settings.ledger_path, daily_cap=settings.daily_usd_cap,
                      total_cap=settings.total_usd_cap, session_cap=settings.session_usd_cap)


async def no_sleep(_seconds):
    return None
