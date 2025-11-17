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


class JellyfinClient:
    def __init__(self, base_url: Optional[str], api_key: Optional[str], session: aiohttp.ClientSession, device_id: str):
        self._base_url = base_url.rstrip("/") if base_url else None
        self._api_key = api_key
        self._session = session
        self._device_id = device_id

    def can_query_users(self) -> bool:
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
