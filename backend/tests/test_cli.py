"""The installed command stays local-only."""

from typing import Any

from app import cli


def test_serve_binds_only_to_loopback(monkeypatch: Any) -> None:
    called: dict[str, object] = {}

    def fake_run(app: str, **kwargs: object) -> None:
        called.update(app=app, **kwargs)

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    assert cli.main(["serve", "--port", "8123"]) == 0
    assert called == {
        "app": "app.main:app",
        "host": "127.0.0.1",
        "port": 8123,
    }
