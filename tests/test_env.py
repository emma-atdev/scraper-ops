import os

from app.env import load_dotenv


def test_load_dotenv_sets_missing_values(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        'SCRAPER_ENVIRONMENT=vm\nQUOTED_VALUE="hello"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("SCRAPER_ENVIRONMENT", raising=False)
    monkeypatch.delenv("QUOTED_VALUE", raising=False)

    load_dotenv(env_file)

    assert os.environ["SCRAPER_ENVIRONMENT"] == "vm"
    assert os.environ["QUOTED_VALUE"] == "hello"


def test_load_dotenv_does_not_override_existing(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("SCRAPER_ENVIRONMENT=vm\n", encoding="utf-8")
    monkeypatch.setenv("SCRAPER_ENVIRONMENT", "local")

    load_dotenv(env_file)

    assert os.environ["SCRAPER_ENVIRONMENT"] == "local"


def test_load_dotenv_missing_file_is_noop(tmp_path):
    load_dotenv(tmp_path / "nope.env")
