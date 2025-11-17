from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class ConfigError(RuntimeError):
    """Raised when the configuration file is missing or malformed."""


@dataclass
class DiscordSettings:
    token: str
    prefix: str = "!"
    activity: Optional[str] = None
    command_aliases: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class JellyfinInstanceSettings:
    name: str
    playback_db: Path
    server_url: Optional[str]
    api_key: Optional[str]


@dataclass
class JellyfinSettings:
    timezone: ZoneInfo
    timezone_name: str
    default_watch_window_days: int
    instances: List[JellyfinInstanceSettings]

    def instance_names(self) -> List[str]:
        return [instance.name for instance in self.instances]

    def default_instance(self) -> JellyfinInstanceSettings:
        return self.instances[0]

    def get_instance(self, name: Optional[str]) -> Optional[JellyfinInstanceSettings]:
        if name is None:
            return self.default_instance()
        for instance in self.instances:
            if instance.name == name:
                return instance
        return None


@dataclass
class LinkingSettings:
    database: Path


@dataclass
class UserMapping:
    discord_id: int
    jellyfin_user_id: str
    display_name: Optional[str] = None
    instance_name: Optional[str] = None


@dataclass
class AppConfig:
    discord: DiscordSettings
    jellyfin: JellyfinSettings
    linking: LinkingSettings
    users: Dict[int, UserMapping]

    def resolve_user(self, discord_id: int) -> Optional[UserMapping]:
        return self.users.get(discord_id)


def _load_timezone(name: Optional[str]) -> tuple[ZoneInfo, str]:
    tz_name = name or "UTC"
    try:
        return ZoneInfo(tz_name), tz_name
    except ZoneInfoNotFoundError as exc:  # pragma: no cover
        raise ConfigError(f"Unknown timezone '{tz_name}'") from exc


def _resolve_secret(
    *,
    label: str,
    literal_value: Optional[str],
    env_var: Optional[str],
    required: bool,
) -> Optional[str]:
    if literal_value and env_var:
        raise ConfigError(f"{label} must provide either a literal value or environment variable, not both")

    if env_var:
        env_value = os.getenv(env_var)
        if env_value:
            return env_value.strip()
        if required:
            raise ConfigError(f"Environment variable '{env_var}' for {label} is not set")
        return None

    if literal_value:
        literal_value = str(literal_value).strip()
        if literal_value:
            return literal_value

    if required:
        raise ConfigError(f"{label} is required")
    return None


def _load_alias_map(raw_aliases: Dict[str, Any]) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    for command_name, aliases in raw_aliases.items():
        cmd_key = str(command_name).strip().lower()
        if not cmd_key:
            continue

        collected: List[str] = []
        if isinstance(aliases, str):
            collected = [aliases]
        else:
            try:
                iterator = list(aliases)  # type: ignore[arg-type]
            except TypeError as exc:  # pragma: no cover
                raise ConfigError(f"Aliases for '{command_name}' must be a string or list of strings") from exc
            for alias in iterator:
                alias_str = str(alias).strip()
                if alias_str:
                    collected.append(alias_str)

        filtered = [alias for alias in (a.lower() for a in collected) if alias]
        if filtered:
            result[cmd_key] = filtered

    return result


def load_config(path: Path | str) -> AppConfig:
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise ConfigError(f"Config file not found: {cfg_path}")

    with cfg_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    try:
        discord_raw = raw["discord"]
        jellyfin_raw = raw["jellyfin"]
    except KeyError as exc:
        raise ConfigError(f"Missing config section: {exc}") from exc

    token = _resolve_secret(
        label="discord.token",
        literal_value=discord_raw.get("token"),
        env_var=discord_raw.get("token_env"),
        required=True,
    )

    alias_raw = discord_raw.get("command_aliases", {})
    if not isinstance(alias_raw, dict):
        raise ConfigError("discord.command_aliases must be a mapping of command names to aliases")
    alias_map = _load_alias_map(alias_raw)

    discord_cfg = DiscordSettings(
        token=token,
        prefix=discord_raw.get("prefix", "!"),
        activity=discord_raw.get("activity"),
        command_aliases=alias_map,
    )

    tz_obj, tz_name = _load_timezone(jellyfin_raw.get("timezone"))
    default_watch_window = int(jellyfin_raw.get("default_watch_window_days", 30))

    instances_raw = jellyfin_raw.get("instances")
    instances: List[JellyfinInstanceSettings] = []
    if instances_raw:
        if not isinstance(instances_raw, list):
            raise ConfigError("jellyfin.instances must be a list")
        for idx, entry in enumerate(instances_raw):
            if not isinstance(entry, dict):
                raise ConfigError("Each jellyfin.instances entry must be a mapping")
            instance_name = str(entry.get("name") or f"instance_{idx + 1}").strip()
            if not instance_name:
                raise ConfigError("Each jellyfin instance must have a non-empty name")
            playback_db = entry.get("playback_db") or jellyfin_raw.get("playback_db")
            if not playback_db:
                raise ConfigError(f"jellyfin.instances[{instance_name}].playback_db is required")
            server_url = (entry.get("server_url") or jellyfin_raw.get("server_url")) or None
            if server_url:
                server_url = server_url.rstrip("/")
            api_key = _resolve_secret(
                label=f"jellyfin.instances[{instance_name}].api_key",
                literal_value=entry.get("api_key"),
                env_var=entry.get("api_key_env"),
                required=False,
            )
            instances.append(
                JellyfinInstanceSettings(
                    name=instance_name,
                    playback_db=Path(playback_db).expanduser().resolve(),
                    server_url=server_url,
                    api_key=api_key,
                )
            )
    else:
        playback_db = jellyfin_raw.get("playback_db")
        if not playback_db:
            raise ConfigError("jellyfin.playback_db is required")
        server_url = jellyfin_raw.get("server_url")
        if server_url:
            server_url = server_url.rstrip("/")
        api_key = _resolve_secret(
            label="jellyfin.api_key",
            literal_value=jellyfin_raw.get("api_key"),
            env_var=jellyfin_raw.get("api_key_env"),
            required=False,
        )
        instances.append(
            JellyfinInstanceSettings(
                name=str(jellyfin_raw.get("name") or "primary"),
                playback_db=Path(playback_db).expanduser().resolve(),
                server_url=server_url,
                api_key=api_key,
            )
        )

    seen_names = set()
    for instance in instances:
        key = instance.name.lower()
        if key in seen_names:
            raise ConfigError(f"Duplicate jellyfin instance name: {instance.name}")
        seen_names.add(key)

    jellyfin_cfg = JellyfinSettings(
        timezone=tz_obj,
        timezone_name=tz_name,
        default_watch_window_days=default_watch_window,
        instances=instances,
    )

    linking_raw = raw.get("linking", {})
    link_db = linking_raw.get("database", "watchtime_links.db")
    linking_cfg = LinkingSettings(database=Path(link_db).expanduser().resolve())

    users_raw = raw.get("users", {})
    users: Dict[int, UserMapping] = {}
    for discord_id_str, entry in users_raw.items():
        if "jellyfin_user_id" not in entry:
            raise ConfigError(f"Missing jellyfin_user_id for Discord user {discord_id_str}")
        try:
            discord_id = int(discord_id_str)
        except ValueError as exc:
            raise ConfigError(f"Invalid Discord user id: {discord_id_str}") from exc

        instance_name = entry.get("instance") or jellyfin_cfg.default_instance().name
        if jellyfin_cfg.get_instance(instance_name) is None:
            raise ConfigError(
                f"Unknown jellyfin instance '{instance_name}' for Discord user {discord_id_str}. "
                f"Available: {', '.join(jellyfin_cfg.instance_names())}"
            )
        users[discord_id] = UserMapping(
            discord_id=discord_id,
            jellyfin_user_id=str(entry["jellyfin_user_id"]),
            display_name=entry.get("display_name"),
            instance_name=instance_name,
        )

    return AppConfig(discord=discord_cfg, jellyfin=jellyfin_cfg, linking=linking_cfg, users=users)
