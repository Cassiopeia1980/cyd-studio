"""Write configurations into the ESPHome Device Builder directory (blocking – run in the executor).

Rules (docs/cyd-studio-prompt.md, section 9):
- a file that was not written by CYD Studio is only replaced after confirmation, with a backup
- a CYD Studio file that was edited by hand (checksum mismatch) shows a diff first, then backup
- secrets.yaml: only wifi_ssid / wifi_password are added or replaced, nothing else is touched
"""

from __future__ import annotations

import difflib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .generator.generate import CHECKSUM_PREFIX, body_checksum, split_generated

SECRET_KEYS = ("wifi_ssid", "wifi_password")
MAX_DIFF_LINES = 400


def config_path(esphome_dir: str, device_name: str) -> Path:
    """Path of the device configuration."""
    return Path(esphome_dir) / f"{device_name}.yaml"


def inspect_existing(esphome_dir: str, device_name: str, new_yaml: str) -> dict[str, Any]:
    """State of the target file: none / ours / modified (with diff) / foreign."""
    path = config_path(esphome_dir, device_name)
    old = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
    return classify_existing(str(path), old, new_yaml)


def classify_existing(path: str, old: str | None, new_yaml: str) -> dict[str, Any]:
    """State of an existing configuration text (None = no file): none / ours / modified / foreign."""
    if old is None:
        return {"state": "none", "path": path}
    if CHECKSUM_PREFIX not in old:
        return {"state": "foreign", "path": path}
    body, checksum = split_generated(old)
    if checksum == body_checksum(body):
        return {"state": "ours", "path": path}
    new_body, _ = split_generated(new_yaml)
    diff = list(
        difflib.unified_diff(
            new_body.splitlines(), body.splitlines(), "CYD Studio", "ESPHome (manuell geändert)", lineterm=""
        )
    )
    return {"state": "modified", "path": path, "diff": "\n".join(diff[:MAX_DIFF_LINES])}


def write_config(esphome_dir: str, device_name: str, yaml_text: str, backup: bool) -> str | None:
    """Write the configuration; optionally keep a timestamped backup. Returns the backup path."""
    path = config_path(esphome_dir, device_name)
    backup_path = None
    if backup and path.is_file():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = path.with_name(f"{path.name}.bak-{stamp}")
        backup_path.write_bytes(path.read_bytes())
    path.write_text(yaml_text, encoding="utf-8", newline="\n")
    return str(backup_path) if backup_path else None


def missing_secrets(esphome_dir: str) -> list[str]:
    """Which of wifi_ssid / wifi_password are missing in secrets.yaml."""
    path = Path(esphome_dir) / "secrets.yaml"
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    return [key for key in SECRET_KEYS if not re.search(rf"^{key}\s*:", text, re.MULTILINE)]


def set_secrets(esphome_dir: str, values: dict[str, str]) -> None:
    """Add or replace only the given keys in secrets.yaml (WiFi and API key), keeping everything else as it is."""
    path = Path(esphome_dir) / "secrets.yaml"
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    lines = text.splitlines()
    for key, value in values.items():
        line = f"{key}: {json.dumps(value, ensure_ascii=False)}"
        for i, existing in enumerate(lines):
            if re.match(rf"^{re.escape(key)}\s*:", existing):
                lines[i] = line
                break
        else:
            lines.append(line)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
