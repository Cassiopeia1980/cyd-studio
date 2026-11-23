"""Write configurations to an ESPHome Device Builder that runs on another machine (WebSocket ``/ws``).

Protocol as used by the ESPHome Device Builder (ESPHome 2026.x), taken over from the ESPHome Studio project:
- every command is ``{"command", "message_id", "args"}``, the answer carries the same ``message_id`` and ``result``
- ``auth/login`` when the builder is protected with a user name and password
- ``devices/get_config``, ``devices/create`` (``overwrite``), ``config/get_secrets`` (key names only) and
  ``config/set_secret``

The builder never reveals secret values, so an existing key is never overwritten here.
Pictures cannot be transferred this way; the caller reports them as missing.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

BASE_RE = re.compile(r"^https?://[^\s/]+(:\d+)?$")
CONNECT_TIMEOUT = 15
ANSWER_TIMEOUT = 20
MAX_YAML = 1_000_000


class DashboardError(Exception):
    """Error with a readable message (shown in the panel)."""


Work = Callable[[Callable[..., Awaitable[Any]]], Awaitable[Any]]


def normalize_url(url: str) -> str:
    """``http://host:6052`` without a path; raises DashboardError for anything else."""
    base = (url or "").strip().rstrip("/")
    if not BASE_RE.match(base):
        raise DashboardError("Invalid ESPHome address (example: http://192.168.2.33:6052, without a path).")
    return base


async def _with_dashboard(session: Any, url: str, username: str, password: str, work: Work) -> Any:
    """Connect, log in if needed and run ``work(cmd)``."""
    base = normalize_url(url)
    try:
        async with session.ws_connect(f"{base}/ws", timeout=CONNECT_TIMEOUT) as ws:
            counter = 0
            needs_auth = False

            async def cmd(command: str, **args: Any) -> Any:
                nonlocal counter, needs_auth
                counter += 1
                mid = str(counter)
                await ws.send_json({"command": command, "message_id": mid, "args": args})
                while True:
                    msg = await asyncio.wait_for(ws.receive_json(), ANSWER_TIMEOUT)
                    if msg.get("message_id") != mid:
                        needs_auth = needs_auth or bool(msg.get("requires_auth"))
                        continue
                    if "result" in msg:
                        return msg["result"]
                    raise DashboardError(
                        f"ESPHome reports: {msg.get('details') or msg.get('message') or msg.get('error') or msg}"
                    )

            if username:
                try:
                    await cmd("auth/login", username=username, password=password)
                except DashboardError as err:
                    raise DashboardError(f"Login to ESPHome failed, check user name and password ({err}).") from err
            try:
                return await work(cmd)
            except DashboardError as err:
                if needs_auth and not username:
                    raise DashboardError(
                        "ESPHome asks for a login: enter user name and password in the options."
                    ) from err
                raise
    except DashboardError:
        raise
    except Exception as err:
        raise DashboardError(
            f"ESPHome is not reachable or answers unexpectedly ({type(err).__name__}). Check the address."
        ) from err


async def check(session: Any, url: str, username: str = "", password: str = "") -> list[str]:
    """Reach the builder; returns the names of the known secrets."""

    async def work(cmd: Callable[..., Awaitable[Any]]) -> list[str]:
        return [str(k) for k in (await cmd("config/get_secrets") or [])]

    return await _with_dashboard(session, url, username, password, work)


async def read_config(session: Any, url: str, name: str, username: str = "", password: str = "") -> str | None:
    """Text of ``<name>.yaml`` or None when the file does not exist yet."""

    async def work(cmd: Callable[..., Awaitable[Any]]) -> str | None:
        try:
            old = await cmd("devices/get_config", configuration=f"{name}.yaml")
        except DashboardError:
            return None  # no such file yet
        return old if isinstance(old, str) and old.strip() and len(old) <= MAX_YAML else None

    return await _with_dashboard(session, url, username, password, work)


async def write_config(
    session: Any,
    url: str,
    name: str,
    yaml_text: str,
    secrets: dict[str, str],
    username: str = "",
    password: str = "",
) -> dict[str, list[str]]:
    """Write ``<name>.yaml`` and add the given secrets that do not exist yet.

    Returns ``secrets_added``, ``secrets_existing`` (key exists, its value is unknown and left alone) and
    ``secrets_known`` (all key names in secrets.yaml afterwards).
    """

    async def work(cmd: Callable[..., Awaitable[Any]]) -> dict[str, list[str]]:
        known = {str(k) for k in (await cmd("config/get_secrets") or [])}
        await cmd("devices/create", name=name, file_content=yaml_text, overwrite=True)
        added: list[str] = []
        existing: list[str] = []
        for key, value in secrets.items():
            if key in known:
                existing.append(key)
            else:
                await cmd("config/set_secret", key=key, value=value)
                added.append(key)
        return {"secrets_added": added, "secrets_existing": existing, "secrets_known": sorted(known | set(added))}

    return await _with_dashboard(session, url, username, password, work)


async def set_secrets(session: Any, url: str, values: dict[str, str], username: str = "", password: str = "") -> None:
    """Set the given secrets (the user entered them on purpose, so existing keys are replaced)."""

    async def work(cmd: Callable[..., Awaitable[Any]]) -> None:
        for key, value in values.items():
            await cmd("config/set_secret", key=key, value=value)

    await _with_dashboard(session, url, username, password, work)
