from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml


@dataclass
class InstanceInput:
    name: str
    playback_db: str
    server_url: Optional[str]
    api_key_env: Optional[str]


class OnboardingWriter:
    """Creates the initial watchtimebot.yaml and .env entries from UI input."""

    def __init__(self, config_path: Path, env_path: Path):
        self._config_path = config_path
        self._env_path = env_path

    def write_config(
        self,
        *,
        discord_token_env: str,
        prefix: str,
        activity: Optional[str],
        timezone: str,
        default_watch_window_days: int,
        link_db: str,
        instances: List[InstanceInput],
    ) -> None:
        data: Dict[str, object] = {
            "discord": {
                "token_env": discord_token_env,
                "prefix": prefix,
                "activity": activity or None,
                "command_aliases": {},
            },
            "jellyfin": {
                "timezone": timezone,
                "default_watch_window_days": default_watch_window_days,
                "instances": [self._instance_to_mapping(instance) for instance in instances],
            },
            "linking": {"database": link_db},
            "users": {},
        }
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        with self._config_path.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, sort_keys=False)

    def update_env(self, entries: Dict[str, str]) -> None:
        values = {key: value for key, value in entries.items() if value}
        if not values:
            return
        current = self._load_env()
        current.update(values)
        with self._env_path.open("w", encoding="utf-8") as handle:
            for key, value in current.items():
                handle.write(f"{key}={self._quote(value)}\n")

    def _load_env(self) -> Dict[str, str]:
        if not self._env_path.exists():
            self._env_path.parent.mkdir(parents=True, exist_ok=True)
            return {}
        entries: Dict[str, str] = {}
        with self._env_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw or raw.startswith("#"):
                    continue
                if "=" not in raw:
                    continue
                key, value = raw.split("=", 1)
                entries[key.strip()] = self._unquote(value.strip())
        return entries

    @staticmethod
    def _unquote(value: str) -> str:
        if value.startswith("\"") and value.endswith("\""):
            inner = value[1:-1]
            return inner.replace("\\\"", '"').replace("\\\\", "\\")
        return value

    @staticmethod
    def _quote(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace("\"", "\\\"")
        return f'"{escaped}"'

    @staticmethod
    def _instance_to_mapping(instance: InstanceInput) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "name": instance.name,
            "playback_db": instance.playback_db,
        }
        if instance.server_url:
            payload["server_url"] = instance.server_url
        if instance.api_key_env:
            payload["api_key_env"] = instance.api_key_env
        return payload
