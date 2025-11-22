from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import aiohttp

from . import __version__ as WATCHTIMEBOT_VERSION


class JellyfinClientError(RuntimeError):
    """Raised when Jellyfin API calls fail."""


@dataclass
class JellyfinUser:
    user_id: str
    username: str
    display_name: Optional[str]


@dataclass
class JellyfinItem:
    item_id: str
    name: str
    item_type: Optional[str]
    overview: Optional[str]
    premiere_year: Optional[int]
    imdb_id: Optional[str]
    tmdb_id: Optional[str]
    tvdb_id: Optional[str]
    anidb_id: Optional[str]
    anilist_id: Optional[str]
    mal_id: Optional[str]
    shoko_id: Optional[str]
    series_id: Optional[str]
    series_name: Optional[str]
    season_number: Optional[int]
    episode_number: Optional[int]
    external_urls: list[str]
    image_url: Optional[str]


class JellyfinClient:
    def __init__(self, base_url: Optional[str], api_key: Optional[str], session: aiohttp.ClientSession, device_id: str):
        self._base_url = base_url.rstrip("/") if base_url else None
        self._api_key = api_key
        self._session = session
        self._device_id = device_id

    def can_query_users(self) -> bool:
        return bool(self._base_url and self._api_key)

    def can_fetch_items(self) -> bool:
        return bool(self._base_url and self._api_key)

    async def fetch_users(self) -> List[JellyfinUser]:
        if not self.can_query_users():
            raise JellyfinClientError(
                "Jellyfin server URL or API key missing. Set jellyfin.server_url and jellyfin.api_key."
            )

        url = f"{self._base_url}/Users"
        async with self._session.get(url, headers=self._request_headers()) as response:
            await _raise_for_status(response, "Failed to fetch users")
            payload = await response.json()

        users: List[JellyfinUser] = []
        for entry in payload:
            user_id = entry.get("Id")
            name = entry.get("Name") or entry.get("DisplayName")
            if not user_id or not name:
                continue
            users.append(
                JellyfinUser(
                    user_id=str(user_id),
                    username=str(name),
                    display_name=entry.get("DisplayName"),
                )
            )
        return users

    async def fetch_item(self, item_id: str, *, include_parents: bool = True) -> JellyfinItem:
        if not self.can_fetch_items():
            raise JellyfinClientError(
                "Jellyfin server URL or API key missing. Set jellyfin.server_url and jellyfin.api_key."
            )

        params = {
            "Fields": (
                "Overview,ProviderIds,ExternalUrls,PremiereDate,ProductionYear,ImageTags,"
                "SeriesId,SeriesName,ParentIndexNumber,IndexNumber"
            ),
            "Ids": item_id,
            "Limit": 1,
        }
        url = f"{self._base_url}/Items"
        async with self._session.get(url, params=params, headers=self._request_headers()) as response:
            await _raise_for_status(response, "Failed to fetch item")
            payload = await response.json()

        items = payload.get("Items") or []
        if not items:
            raise JellyfinClientError(f"Failed to fetch item: not found ({item_id})")
        data = items[0]

        provider_ids = data.get("ProviderIds") or {}
        imdb_id = provider_ids.get("Imdb") or provider_ids.get("ImdbId")
        tmdb_id = provider_ids.get("Tmdb") or provider_ids.get("TmdbId")
        tvdb_id = provider_ids.get("Tvdb") or provider_ids.get("TvdbId")
        anidb_id = provider_ids.get("AniDB") or provider_ids.get("Anidb") or provider_ids.get("AniDb")
        anilist_id = provider_ids.get("AniList") or provider_ids.get("Anilist")
        mal_id = provider_ids.get("MyAnimeList") or provider_ids.get("Mal")
        shoko_id = (
            provider_ids.get("ShokoEpisode")
            or provider_ids.get("ShokoSeries")
            or provider_ids.get("Shoko")
            or provider_ids.get("ShokoId")
        )
        external_urls_raw = data.get("ExternalUrls") or []
        external_urls: list[str] = []
        for entry in external_urls_raw:
            url_value = entry.get("Url")
            if url_value:
                external_urls.append(str(url_value))

        item_type = data.get("Type")
        premiere_year = data.get("ProductionYear")
        if premiere_year is None:
            premiere_date = data.get("PremiereDate")
            if isinstance(premiere_date, str) and len(premiere_date) >= 4:
                try:
                    premiere_year = int(premiere_date[:4])
                except ValueError:
                    premiere_year = None

        image_url = None
        primary_tag = (data.get("ImageTags") or {}).get("Primary")
        if primary_tag and self._base_url and self._api_key:
            image_url = (
                f"{self._base_url}/Items/{item_id}/Images/Primary"
                f"?tag={primary_tag}&quality=90&maxHeight=800&api_key={self._api_key}"
            )

        return JellyfinItem(
            item_id=str(item_id),
            name=str(data.get("Name") or data.get("OriginalTitle") or "Unknown"),
            item_type=item_type,
            overview=data.get("Overview"),
            premiere_year=premiere_year,
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
            tvdb_id=tvdb_id,
            anidb_id=anidb_id,
            anilist_id=anilist_id,
            mal_id=mal_id,
            shoko_id=shoko_id,
            series_id=data.get("SeriesId"),
            series_name=data.get("SeriesName"),
            season_number=data.get("ParentIndexNumber"),
            episode_number=data.get("IndexNumber"),
            external_urls=external_urls,
            image_url=image_url,
        )

    def _request_headers(self, override_token: Optional[str] = None) -> dict[str, str]:
        token = override_token or self._api_key
        token_fragment = f', Token="{token}"' if token else ""
        return {
            "X-Emby-Authorization": (
                'MediaBrowser Client="WatchtimeBot", '
                'Device="WatchtimeBot", '
                f'DeviceId="{self._device_id}", '
                f'Version="{WATCHTIMEBOT_VERSION}"'
                f"{token_fragment}"
            ),
            "Accept": "application/json",
        }


async def _raise_for_status(response: aiohttp.ClientResponse, context: str) -> None:
    if response.status < 400:
        return
    if response.status == 401:
        raise JellyfinClientError(f"{context}: unauthorized (401).")
    text = await response.text()
    raise JellyfinClientError(f"{context}: HTTP {response.status} — {text[:200]}")
