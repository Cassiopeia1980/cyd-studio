"""ESPHome Device Builder on another machine: protocol against a fake WebSocket (no network, no Home Assistant)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from esphome_dashboard import DashboardError, check, normalize_url, read_config, set_secrets, write_config


class FakeBuilder:
    """Answers the commands of the Device Builder; remembers files and secrets."""

    def __init__(self, password: str | None = None) -> None:
        self.files: dict[str, str] = {}
        self.secrets: dict[str, str] = {"wifi_ssid": "x"}
        self.password = password
        self.logged_in = password is None
        self.sent: list[dict[str, Any]] = []
        self.queue: list[dict[str, Any]] = [{"event": "hello", "esphome_version": "2026.11.0"}]

    # aiohttp-like session / websocket
    def ws_connect(self, url: str, timeout: int) -> FakeBuilder:
        self.url = url
        return self

    async def __aenter__(self) -> FakeBuilder:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sent.append(data)
        mid, cmd, args = data["message_id"], data["command"], data["args"]
        if cmd == "auth/login":
            if args["password"] == self.password:
                self.logged_in = True
                self.queue.append({"message_id": mid, "result": True})
            else:
                self.queue.append({"message_id": mid, "error": "invalid", "details": "bad login"})
        elif not self.logged_in:
            self.queue.append({"requires_auth": True})
            self.queue.append({"message_id": mid, "error": "auth", "details": "authentication required"})
        elif cmd == "config/get_secrets":
            self.queue.append({"message_id": mid, "result": list(self.secrets)})
        elif cmd == "config/set_secret":
            self.secrets[args["key"]] = args["value"]
            self.queue.append({"message_id": mid, "result": True})
        elif cmd == "devices/get_config":
            if args["configuration"] in self.files:
                self.queue.append({"message_id": mid, "result": self.files[args["configuration"]]})
            else:
                self.queue.append({"message_id": mid, "error": "not_found", "details": "no such file"})
        elif cmd == "devices/create":
            self.files[f"{args['name']}.yaml"] = args["file_content"]
            self.queue.append({"message_id": mid, "result": True})

    async def receive_json(self) -> dict[str, Any]:
        return self.queue.pop(0)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_normalize_url() -> None:
    assert normalize_url(" http://192.168.2.33:6052/ ") == "http://192.168.2.33:6052"
    for bad in ("", "192.168.2.33", "http://host/path", "ftp://host"):
        with pytest.raises(DashboardError):
            normalize_url(bad)


def test_write_adds_only_missing_secrets() -> None:
    builder = FakeBuilder()
    secrets = {"wifi_ssid": "ignored", "api_key_labor_touch": "KEY"}
    out = run(write_config(builder, "http://192.168.2.33:6052", "labor-touch", "esphome:\n", secrets))
    assert builder.url == "http://192.168.2.33:6052/ws"
    assert builder.files["labor-touch.yaml"] == "esphome:\n"
    assert out["secrets_added"] == ["api_key_labor_touch"]
    assert out["secrets_existing"] == ["wifi_ssid"]
    assert builder.secrets["wifi_ssid"] == "x"  # existing value untouched
    assert builder.secrets["api_key_labor_touch"] == "KEY"
    assert set(out["secrets_known"]) == {"wifi_ssid", "api_key_labor_touch"}


def test_read_config_missing_and_present() -> None:
    builder = FakeBuilder()
    assert run(read_config(builder, "http://h:6052", "a")) is None
    builder.files["a.yaml"] = "x: 1\n"
    assert run(read_config(builder, "http://h:6052", "a")) == "x: 1\n"


def test_login() -> None:
    builder = FakeBuilder(password="pw")
    with pytest.raises(DashboardError, match="login"):
        run(check(builder, "http://h:6052"))
    with pytest.raises(DashboardError, match="Login"):
        run(check(builder, "http://h:6052", "me", "wrong"))
    assert run(check(builder, "http://h:6052", "me", "pw")) == ["wifi_ssid"]


def test_set_secrets_replaces() -> None:
    builder = FakeBuilder()
    run(set_secrets(builder, "http://h:6052", {"wifi_ssid": "Neu", "wifi_password": "pw"}))
    assert builder.secrets == {"wifi_ssid": "Neu", "wifi_password": "pw"}


def test_unreachable_is_readable() -> None:
    class Down:
        def ws_connect(self, url: str, timeout: int) -> Any:
            raise OSError("no route")

    with pytest.raises(DashboardError, match="not reachable"):
        run(check(Down(), "http://h:6052"))
