from __future__ import annotations

import logging
import os
import socket
from dataclasses import dataclass
from datetime import tzinfo
from typing import Dict, Optional

import aiohttp
import discord
from discord.ext import commands

from .config_loader import (
    AppConfig,
    ConfigError,
    JellyfinInstanceSettings,
    UserMapping,
    load_config,
)
from .jellyfin_client import JellyfinClient
from .jellyfin_reporting import PlaybackEntry, PlaybackReportingStore
from .link_store import UserLinkStore
from .linking import LinkingCog
from .time_format import format_discord_timestamp, format_duration, humanize_datetime

log = logging.getLogger(__name__)
CONFIG_ENV = "WATCHTIMEBOT_CONFIG"
DEFAULT_CONFIG_PATH = "watchtimebot.yaml"


@dataclass
class _InstanceRuntime:
    config: JellyfinInstanceSettings
    store: PlaybackReportingStore
    client: Optional[JellyfinClient] = None


class WatchtimeBot(commands.Bot):
    def __init__(self, config: AppConfig):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=config.discord.prefix, intents=intents)
        self.config = config
        self.instances: Dict[str, _InstanceRuntime] = {}
        for instance_cfg in config.jellyfin.instances:
            self.instances[instance_cfg.name] = _InstanceRuntime(
                config=instance_cfg,
                store=PlaybackReportingStore(instance_cfg.playback_db),
            )
        self.default_instance_name = config.jellyfin.default_instance().name
        self.link_store = UserLinkStore(config.linking.database)
        self.http_session: Optional[aiohttp.ClientSession] = None
        self.jellyfin_client: Optional[JellyfinClient] = None
        self._device_id = os.getenv("WATCHTIMEBOT_DEVICE_ID") or socket.gethostname() or "watchtimebot"

    async def setup_hook(self) -> None:
        for runtime in self.instances.values():
            await runtime.store.connect()
        await self.link_store.connect()
        self.http_session = aiohttp.ClientSession()
        for runtime in self.instances.values():
            runtime.client = JellyfinClient(
                base_url=runtime.config.server_url,
                api_key=runtime.config.api_key,
                session=self.http_session,
                device_id=self._device_id,
            )
        self.jellyfin_client = self.get_instance_client(None)
        await self.add_cog(PlaybackCog(self))
        await self.add_cog(LinkingCog(self))
        self._apply_command_aliases()

    async def close(self) -> None:
        for runtime in self.instances.values():
            await runtime.store.close()
        await self.link_store.close()
        if self.http_session:
            await self.http_session.close()
        await super().close()

    def _apply_command_aliases(self) -> None:
        alias_map = self.config.discord.command_aliases
        if not alias_map:
            return

        for command_name, aliases in alias_map.items():
            command = self.get_command(command_name)
            if command is None:
                log.warning("Unknown command '%s' referenced in command_aliases", command_name)
                continue

            sanitized: list[str] = []
            for alias in aliases:
                alias_clean = alias.strip().lower()
                if not alias_clean or alias_clean == command.name or alias_clean in sanitized:
                    continue
                sanitized.append(alias_clean)

            if not sanitized:
                continue

            for existing_alias in list(command.aliases):
                self.all_commands.pop(existing_alias, None)

            final_aliases: list[str] = []
            for alias_name in sanitized:
                existing_command = self.all_commands.get(alias_name)
                if existing_command and existing_command is not command:
                    log.warning(
                        "Alias '%s' conflicts with existing command '%s'; skipping",
                        alias_name,
                        existing_command.qualified_name,
                    )
                    continue
                final_aliases.append(alias_name)

            command.aliases = final_aliases
            for alias_name in final_aliases:
                self.all_commands[alias_name] = command

    def get_instance_runtime(self, instance_name: Optional[str]) -> Optional[_InstanceRuntime]:
        name = (instance_name or self.default_instance_name)
        return self.instances.get(name)

    def get_instance_client(self, instance_name: Optional[str]) -> Optional[JellyfinClient]:
        runtime = self.get_instance_runtime(instance_name)
        if runtime is None:
            return None
        return runtime.client


class PlaybackCog(commands.Cog):
    def __init__(self, bot: WatchtimeBot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        activity = self.bot.config.discord.activity
        if activity:
            await self.bot.change_presence(activity=discord.Game(name=activity))
        log.info("Logged in as %s", self.bot.user)

    @commands.command(name="watchtime")
    async def watchtime(self, ctx: commands.Context, window: Optional[str] = None) -> None:
        mapping = await self._resolve_mapping(ctx)
        if mapping is None:
            return

        runtime = self.bot.get_instance_runtime(mapping.instance_name)
        if runtime is None:
            await ctx.reply("Your Jellyfin instance is not configured on this bot.")
            return

        days: Optional[int]
        if window is None:
            days = self.bot.config.jellyfin.default_watch_window_days
        else:
            window_clean = window.lower()
            if window_clean in {"all", "lifetime", "total"}:
                days = None
            else:
                try:
                    days = max(1, int(window_clean))
                except ValueError:
                    await ctx.reply("Please pass an integer number of days or 'all'.")
                    return

        seconds = await runtime.store.get_watchtime_seconds(mapping.jellyfin_user_id, days)
        if days is None:
            label = "all time"
        else:
            label = f"the last {days} day{'s' if days != 1 else ''}"

        total_hours = seconds / 3600 if seconds else 0
        await ctx.reply(
            f"{mapping.display_name or ctx.author.display_name} has logged "
            f"{total_hours:.2f}h of Jellyfin playback in {label}."
        )

    @commands.command(name="lastwatched")
    async def last_watched(self, ctx: commands.Context) -> None:
        mapping = await self._resolve_mapping(ctx)
        if mapping is None:
            return

        runtime = self.bot.get_instance_runtime(mapping.instance_name)
        if runtime is None:
            await ctx.reply("Your Jellyfin instance is not configured on this bot.")
            return

        entry = await runtime.store.get_last_play(mapping.jellyfin_user_id)
        if not entry:
            await ctx.reply("No playback history found for you yet.")
            return

        timestamp = format_discord_timestamp(entry.started_at, self.bot.config.jellyfin.timezone)
        await ctx.reply(
            f"Last watched: **{entry.item_name}** ({entry.item_type or 'Unknown'})\n"
            f"Started at {timestamp} via {entry.client_name or 'unknown client'}"
        )

    @commands.command(name="recentplays", aliases=["recent"])
    async def recent_plays(self, ctx: commands.Context, count: Optional[int] = None) -> None:
        mapping = await self._resolve_mapping(ctx)
        if mapping is None:
            return

        runtime = self.bot.get_instance_runtime(mapping.instance_name)
        if runtime is None:
            await ctx.reply("Your Jellyfin instance is not configured on this bot.")
            return

        limit = max(1, min(10, count or 5))
        entries = await runtime.store.get_recent_plays(mapping.jellyfin_user_id, limit)
        if not entries:
            await ctx.reply("No playback history found.")
            return

        lines = [_format_entry(entry, self.bot.config.jellyfin.timezone) for entry in entries]
        await ctx.reply("Recent plays:\n" + "\n".join(lines))

    async def _resolve_mapping(self, ctx: commands.Context) -> Optional[UserMapping]:
        mapping = await self.bot.link_store.get_mapping(ctx.author.id)
        if mapping is None:
            mapping = self.bot.config.resolve_user(ctx.author.id)
        if mapping is None:
            prefix = self.bot.command_prefix
            await ctx.reply(
                "I don't know your Jellyfin user yet. "
                f"Run `{prefix}link` to pair your Jellyfin account or ask an admin to map you."
            )
            return None
        if mapping.instance_name is None:
            mapping.instance_name = self.bot.default_instance_name
        return mapping


def _format_entry(entry: PlaybackEntry, tz: tzinfo) -> str:
    timestamp = humanize_datetime(entry.started_at, tz)
    return (
        f"• {entry.item_name} — {format_duration(entry.duration_seconds)} "
        f"on {timestamp} via {entry.client_name or 'unknown'}"
    )


def _build_config() -> AppConfig:
    cfg_path = os.getenv(CONFIG_ENV, DEFAULT_CONFIG_PATH)
    return load_config(cfg_path)


def run_bot() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")
    try:
        config = _build_config()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc

    bot = WatchtimeBot(config)
    bot.run(config.discord.token)


if __name__ == "__main__":
    run_bot()
