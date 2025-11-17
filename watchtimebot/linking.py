from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence, Tuple

import discord
from discord.ext import commands

from .config_loader import UserMapping
from .jellyfin_client import JellyfinClient, JellyfinClientError, JellyfinUser

USER_CACHE_TTL = timedelta(minutes=5)


def _normalize(value: str) -> str:
    return "".join(ch.lower() for ch in value if ch.isalnum())


def _score(query: str, candidate: str) -> float:
    return SequenceMatcher(None, query, candidate).ratio()


@dataclass
class _UserCache:
    users: List[JellyfinUser]
    expires_at: datetime


class LinkingCog(commands.Cog):
    """Commands that allow Discord users (and admins) to manage Jellyfin links."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache: Dict[str, _UserCache] = {}
        self._lock = asyncio.Lock()
        self._instance_map: Dict[str, str] = {
            name.lower(): name for name in self.bot.config.jellyfin.instance_names()
        }

    @commands.command(name="link")
    async def link(self, ctx: commands.Context, *, query: Optional[str] = None) -> None:
        """
        Link the invoking Discord user to a Jellyfin account via username matching.

        Usage:
            !link <username>                -> attempts to link automatically (default instance).
            !link <instance> <username>     -> target a specific Jellyfin instance.
            !link                           -> sends instructions with tips and available usernames.
        """
        if not query:
            await self._send_instructions(ctx)
            return

        instance_name, username = self._parse_link_query(query)
        runtime = self.bot.get_instance_runtime(instance_name)
        if runtime is None:
            await ctx.reply(self._unknown_instance_message())
            return

        client = runtime.client
        if not client or not client.can_query_users():
            await ctx.reply(
                "Linking is not configured for that Jellyfin instance. "
                "Ask an admin to set server_url and api_key in the config."
            )
            return

        existing = await self.bot.link_store.get_mapping(ctx.author.id)
        if existing:
            await ctx.reply(
                f"You're already linked to a Jellyfin account. Run `{self._prefix()}unlink` first if you need to relink."
            )
            return

        try:
            users = await self._get_users(runtime.config.name)
        except JellyfinClientError as exc:
            await ctx.reply(f"Couldn't reach Jellyfin: {exc}")
            return

        normalized_query = _normalize(username)
        exact = [user for user in users if _normalize(user.username) == normalized_query]
        if not exact:
            exact = [user for user in users if user.display_name and _normalize(user.display_name) == normalized_query]

        if len(exact) == 1:
            await self._finalize_link(ctx, exact[0], runtime.config.name)
            return
        if len(exact) > 1:
            formatted = ", ".join(_format_user(user) for user in exact[:5])
            await ctx.reply(
                "That name matches multiple Jellyfin users. Please specify the exact username or ask an admin:\n"
                f"{formatted}"
            )
            return

        suggestions = self._suggest_users(users, normalized_query)
        if not suggestions:
            await ctx.reply("I couldn't find any Jellyfin user with a similar name. Double-check your username.")
            return

        formatted = ", ".join(_format_user(user) for user in suggestions[:5])
        await ctx.reply(
            "I couldn't find an exact match. Did you mean one of these?\n"
            f"{formatted}\n"
            f"Re-run `{self._prefix()}link <username>` with the exact name."
        )

    @commands.command(name="unlink")
    async def unlink(self, ctx: commands.Context) -> None:
        """Remove the stored Jellyfin link for the invoking user."""
        existing = await self.bot.link_store.get_mapping(ctx.author.id)
        if not existing:
            await ctx.reply("You're not linked to a Jellyfin account yet.")
            return

        await self.bot.link_store.remove_link(ctx.author.id)
        await ctx.reply("Your Jellyfin link has been removed. Run `!link <username>` anytime to start over.")

    @commands.command(name="forcelink")
    @commands.has_permissions(manage_guild=True)
    async def force_link(
        self,
        ctx: commands.Context,
        member: discord.Member,
        jellyfin_user_id: str,
        *,
        display_name: Optional[str] = None,
    ) -> None:
        """Admin shortcut to map a Discord member to a Jellyfin user."""
        try:
            jellyfin_clean, instance_name = self._parse_admin_jellyfin_identifier(jellyfin_user_id)
        except ValueError:
            await ctx.reply(self._unknown_instance_message())
            return
        if instance_name is None:
            instance_name = self.bot.default_instance_name

        runtime = self.bot.get_instance_runtime(instance_name)
        if runtime is None:
            await ctx.reply(self._unknown_instance_message())
            return

        mapping = UserMapping(
            discord_id=member.id,
            jellyfin_user_id=jellyfin_clean,
            display_name=display_name or member.display_name,
            instance_name=instance_name,
        )
        await self.bot.link_store.upsert_link(mapping)
        await ctx.reply(
            f"Linked {member.mention} to Jellyfin user `{mapping.jellyfin_user_id}` "
            f"on `{instance_name}` (display: {mapping.display_name or 'n/a'})."
        )
        await self._dm_safely(
            member,
            f"An admin linked you to Jellyfin user `{mapping.display_name or mapping.jellyfin_user_id}` "
            f"in {ctx.guild.name} (instance: {instance_name}). Run `{self._prefix()}unlink` if this wasn't you.",
        )

    @commands.command(name="forceunlink")
    @commands.has_permissions(manage_guild=True)
    async def force_unlink(self, ctx: commands.Context, member: discord.Member) -> None:
        """Admin shortcut to drop a stored Jellyfin link."""
        existing = await self.bot.link_store.get_mapping(member.id)
        if not existing:
            await ctx.reply(f"{member.mention} is not linked.")
            return

        await self.bot.link_store.remove_link(member.id)
        await ctx.reply(f"Removed the Jellyfin link for {member.mention}.")
        await self._dm_safely(
            member,
            f"An admin removed your Jellyfin link in {ctx.guild.name}. "
            f"Run `{self._prefix()}link <username>` if you want to link again.",
        )

    async def _finalize_link(self, ctx: commands.Context, user: JellyfinUser, instance_name: str) -> None:
        mapping = UserMapping(
            discord_id=ctx.author.id,
            jellyfin_user_id=user.user_id,
            display_name=user.display_name or user.username,
            instance_name=instance_name,
        )
        await self.bot.link_store.upsert_link(mapping)
        await ctx.reply(
            f"Linked! You're now paired with Jellyfin user **{mapping.display_name or mapping.jellyfin_user_id}**. "
            f"Try `{self._prefix()}watchtime` in the server."
        )

    async def _send_instructions(self, ctx: commands.Context) -> None:
        prefix = self._prefix()
        instructions = (
            "To link your Jellyfin account:\n"
            f"1. Find your username in Jellyfin (top-right menu).\n"
            f"2. Run `{prefix}link <your exact username>` here or in DM.\n"
            "If I can't find the name, I'll suggest the closest matches."
        )

        if len(self._instance_map) > 1:
            available = ", ".join(sorted(self._instance_map.values()))
            instructions += (
                "\nMultiple Jellyfin instances are configured: "
                f"{available}. Use `{prefix}link <instance> <username>` to pick one."
            )

        if ctx.guild is None:
            await ctx.reply(instructions)
            return

        try:
            await ctx.author.send(instructions)
            await ctx.reply("Check your DMs for linking instructions.")
        except discord.Forbidden:
            await ctx.reply(instructions)

    async def _get_users(self, instance_name: str) -> Sequence[JellyfinUser]:
        async with self._lock:
            now = datetime.utcnow()
            cached = self._cache.get(instance_name)
            if cached and now < cached.expires_at:
                return cached.users

            client = self.bot.get_instance_client(instance_name)
            if not isinstance(client, JellyfinClient):
                raise JellyfinClientError("Linking is not configured for that Jellyfin instance.")

            users = await client.fetch_users()
            self._cache[instance_name] = _UserCache(users=users, expires_at=now + USER_CACHE_TTL)
            return users

    def _parse_link_query(self, raw: str) -> Tuple[Optional[str], str]:
        cleaned = raw.strip()
        if not cleaned:
            return None, cleaned
        if len(self._instance_map) > 1:
            parts = cleaned.split(None, 1)
            if len(parts) == 2:
                instance_candidate = self._normalize_instance_name(parts[0])
                if instance_candidate:
                    return instance_candidate, parts[1].strip()
        return None, cleaned

    def _parse_admin_jellyfin_identifier(self, value: str) -> Tuple[str, Optional[str]]:
        cleaned = value.strip()
        instance_name: Optional[str] = None
        jellyfin_id = cleaned
        if "::" in cleaned:
            prefix, rest = cleaned.split("::", 1)
            normalized = self._normalize_instance_name(prefix)
            if normalized is None:
                raise ValueError(prefix)
            instance_name = normalized
            jellyfin_id = rest.strip()
        return jellyfin_id, instance_name

    def _normalize_instance_name(self, value: str) -> Optional[str]:
        return self._instance_map.get(value.lower())

    def _unknown_instance_message(self) -> str:
        available = ", ".join(sorted(self._instance_map.values())) or "(none configured)"
        return f"Unknown Jellyfin instance. Available options: {available}."

    def _suggest_users(self, users: Sequence[JellyfinUser], normalized_query: str) -> List[JellyfinUser]:
        scored: List[Tuple[float, JellyfinUser]] = []
        for user in users:
            candidates = [user.username, user.display_name] if user.display_name else [user.username]
            best = max((_score(normalized_query, _normalize(candidate)) for candidate in candidates), default=0.0)
            scored.append((best, user))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [user for score, user in scored if score >= 0.5][:5]

    async def _dm_safely(self, user: Optional[discord.abc.User], message: str) -> None:
        if not user:
            return
        try:
            await user.send(message)
        except discord.Forbidden:
            pass

    def _prefix(self) -> str:
        prefix = self.bot.command_prefix
        if isinstance(prefix, (list, tuple)):
            return prefix[0]
        return str(prefix)


def _format_user(user: JellyfinUser) -> str:
    if user.display_name and user.display_name != user.username:
        return f"{user.username} (display: {user.display_name})"
    return user.username
