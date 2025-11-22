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
from .jellyfin_client import JellyfinClient, JellyfinItem
from .jellyfin_reporting import PlaybackEntry, PlaybackReportingStore
from .link_store import UserLinkStore
from .linking import LinkingCog
from .time_format import format_discord_timestamp, format_duration, humanize_datetime
import urllib.parse

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
        embed = await self._build_last_watched_embed(entry, runtime.client, timestamp)
        if embed is None:
            await ctx.reply(
                f"Last watched: **{entry.item_name}** ({entry.item_type or 'Unknown'})\n"
                f"Started at {timestamp} via {entry.client_name or 'unknown client'}"
            )
            return

        await ctx.reply(embed=embed)

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

    async def _build_last_watched_embed(
        self,
        entry: PlaybackEntry,
        client: Optional[JellyfinClient],
        timestamp: str,
    ) -> Optional[discord.Embed]:
        metadata: Optional[JellyfinItem] = None
        series_meta: Optional[JellyfinItem] = None
        if entry.item_id and client and client.can_fetch_items():
            try:
                metadata = await client.fetch_item(entry.item_id)
                if metadata and metadata.item_type and metadata.item_type.lower() == "episode" and metadata.series_id:
                    try:
                        series_meta = await client.fetch_item(metadata.series_id, include_parents=False)
                    except Exception as exc:  # pragma: no cover
                        log.warning("Failed to fetch series metadata for %s: %s", metadata.series_id, exc)
            except Exception as exc:  # pragma: no cover - best-effort metadata fetch
                log.warning("Failed to fetch item metadata for %s: %s", entry.item_id, exc)

        if metadata is None:
            return None

        link_label, primary_url = _resolve_primary_link(metadata, series_meta)
        description = metadata.overview or ""
        if len(description) > 300:
            description = description[:297].rstrip() + "..."

        title = metadata.name
        if metadata.item_type and metadata.item_type.lower() == "episode" and metadata.series_name:
            if metadata.season_number is not None and metadata.episode_number is not None:
                title = f"{metadata.series_name} — S{metadata.season_number:02d}E{metadata.episode_number:02d} — {metadata.name}"
            else:
                title = f"{metadata.series_name} — {metadata.name}"

        embed = discord.Embed(
            title=title,
            description=description or None,
            colour=discord.Color.blue(),
        )
        if primary_url:
            embed.url = primary_url

        type_label = metadata.item_type or entry.item_type or "Unknown"
        if metadata.item_type and metadata.item_type.lower() == "episode":
            year = series_meta.premiere_year if series_meta else None
        else:
            year = metadata.premiere_year or (series_meta.premiere_year if series_meta else None)
        if year:
            type_label = f"{type_label} ({year})"
        embed.add_field(name="Type", value=type_label, inline=True)
        embed.add_field(name="Watched", value=timestamp, inline=True)

        client_label = entry.client_name or "unknown client"
        if entry.device_name:
            client_label += f" on {entry.device_name}"
        embed.add_field(name="Client", value=client_label, inline=False)

        if primary_url and link_label:
            embed.add_field(name=link_label, value=primary_url, inline=False)
        elif metadata.external_urls:
            embed.add_field(name="Link", value=metadata.external_urls[0], inline=False)

        image_url = _safe_image_url(metadata.image_url)
        if image_url:
            embed.set_image(url=image_url)
        elif metadata.image_url:
            log.debug("Discarding invalid image URL for %s: %s", metadata.item_id, metadata.image_url)

        embed.set_footer(text=f"Started via {entry.client_name or 'unknown client'}")
        return embed


def _format_entry(entry: PlaybackEntry, tz: tzinfo) -> str:
    timestamp = format_discord_timestamp(entry.started_at, tz)
    return (
        f"• {entry.item_name} — {format_duration(entry.duration_seconds)} "
        f"on {timestamp} via {entry.client_name or 'unknown'}"
    )


def _resolve_primary_link(item: JellyfinItem, series: Optional[JellyfinItem] = None) -> tuple[Optional[str], Optional[str]]:
    # Prefer series-level IDs for episodes if present
    chosen = _resolve_link_from_item(series or item)
    if chosen[1]:
        return chosen
    return _resolve_link_from_item(item)


def _resolve_link_from_item(item: JellyfinItem) -> tuple[Optional[str], Optional[str]]:
    # Prefer IMDb, then TMDB, TVDB, AniDB, AniList, MAL, then any external URL
    if item.imdb_id:
        return "IMDb", f"https://www.imdb.com/title/{item.imdb_id}"
    for url in item.external_urls:
        if "imdb.com/title" in url.lower():
            return "IMDb", url

    if item.tmdb_id:
        path = "tv" if (item.item_type or "").lower() in {"series", "show", "tv"} else "movie"
        return "TMDB", f"https://www.themoviedb.org/{path}/{item.tmdb_id}"
    for url in item.external_urls:
        if "themoviedb.org" in url.lower():
            return "TMDB", url

    if item.tvdb_id:
        return "TVDB", f"https://thetvdb.com/?id={item.tvdb_id}"
    for url in item.external_urls:
        if "thetvdb.com" in url.lower():
            return "TVDB", url

    if item.anidb_id:
        return "AniDB", f"https://anidb.net/anime/{item.anidb_id}"
    for url in item.external_urls:
        if "anidb.net" in url.lower():
            return "AniDB", url
    if item.shoko_id:
        return "AniDB (Shoko)", f"https://anidb.net/anime/{item.shoko_id}"

    if item.anilist_id:
        return "AniList", f"https://anilist.co/anime/{item.anilist_id}"
    for url in item.external_urls:
        if "anilist.co" in url.lower():
            return "AniList", url

    if item.mal_id:
        return "MyAnimeList", f"https://myanimelist.net/anime/{item.mal_id}"
    for url in item.external_urls:
        if "myanimelist.net" in url.lower():
            return "MyAnimeList", url

    if item.external_urls:
        return "Link", item.external_urls[0]
    return None, None


def _safe_image_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    if " " in url or "\n" in url:
        return None
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    if parsed.scheme != "https":
        return None
    return url


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
