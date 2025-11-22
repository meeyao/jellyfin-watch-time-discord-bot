from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config_loader import AppConfig, ConfigError, UserMapping, load_config
from .configurator import InstanceInput, OnboardingWriter
from .link_store import UserLinkStore

CONFIG_ENV = "WATCHTIMEBOT_CONFIG"
DEFAULT_CONFIG_PATH = "watchtimebot.yaml"
ENV_FILE_ENV = "WATCHTIMEBOT_ENV_FILE"
DEFAULT_ENV_PATH = ".env"

templates = Jinja2Templates(directory=str(Path(__file__).with_name("templates") / "admin"))


def _preset_from_config(config: Optional[AppConfig]) -> Dict[str, object]:
    if not config:
        return {}
    instances = []
    for instance in config.jellyfin.instances:
        instances.append(
            {
                "name": instance.name,
                "playback_db": instance.playback_db,
                "server_url": instance.server_url,
                # Legacy configs store only the resolved api_key; api_key_env may not exist
                "api_key_env": getattr(instance, "api_key_env", None),
            }
        )
    return {
        "discord_token_env": config.discord.token_env,
        "prefix": config.discord.prefix,
        "activity": config.discord.activity,
        "timezone": config.jellyfin.timezone,
        "watch_window": config.jellyfin.default_watch_window_days,
        "link_db": config.linking.database,
        "instances": instances,
    }


class AdminState:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        env_override = os.getenv(ENV_FILE_ENV)
        self.env_path = Path(env_override) if env_override else config_path.parent / DEFAULT_ENV_PATH
        self.config: Optional[AppConfig] = None
        self.config_error: Optional[str] = None
        self.link_store: Optional[UserLinkStore] = None
        self.onboarding = OnboardingWriter(config_path, self.env_path)
        self.reload_config()

    def reload_config(self) -> None:
        try:
            self.config = load_config(self.config_path)
            self.config_error = None
        except ConfigError as exc:  # pragma: no cover
            self.config = None
            self.config_error = str(exc)

    async def refresh_link_store(self) -> None:
        if self.link_store is not None:
            await self.link_store.close()
            self.link_store = None
        if self.config is None:
            return
        self.link_store = UserLinkStore(self.config.linking.database)
        await self.link_store.connect()

    def is_configured(self) -> bool:
        return self.config is not None and self.config_error is None

    async def get_link_store(self) -> UserLinkStore:
        if not self.is_configured():
            raise HTTPException(status_code=400, detail="Configuration not completed")
        if self.link_store is None:
            self.link_store = UserLinkStore(self.config.linking.database)  # type: ignore[union-attr]
            await self.link_store.connect()
        return self.link_store

    def instance_context(self) -> Dict[str, object]:
        if not self.config:
            return {}
        return {
            "instances": self.config.jellyfin.instance_names(),
            "default_instance": self.config.jellyfin.default_instance().name,
        }


async def get_state(request: Request) -> AdminState:
    return request.app.state.admin_state  # type: ignore[attr-defined]


def create_app(config_path: Optional[str] = None) -> FastAPI:
    cfg_path = Path(config_path or os.getenv(CONFIG_ENV, DEFAULT_CONFIG_PATH))
    state = AdminState(cfg_path)
    app = FastAPI(title="WatchtimeBot Admin", version="0.2.0")
    app.state.admin_state = state

    @app.on_event("startup")
    async def startup() -> None:  # pragma: no cover
        await state.refresh_link_store()

    @app.on_event("shutdown")
    async def shutdown() -> None:  # pragma: no cover
        if state.link_store:
            await state.link_store.close()

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request, state: AdminState = Depends(get_state)) -> HTMLResponse:
        if not state.is_configured():
            return RedirectResponse("/setup", status_code=302)
        store = await state.get_link_store()
        mappings = await store.list_links()
        static_users = list(state.config.users.values()) if state.config else []
        status = request.query_params.get("status")
        focus = request.query_params.get("discord_id")
        context = {
            "request": request,
            "links": mappings,
            "static_users": static_users,
            "status": status,
            "focus": focus,
            "title": "WatchtimeBot Admin",
            "header": "Linked Users",
        }
        context.update(state.instance_context())
        return templates.TemplateResponse("links_list.html", context)

    @app.get("/links/new", response_class=HTMLResponse)
    async def new_link(request: Request, state: AdminState = Depends(get_state)) -> HTMLResponse:
        if not state.is_configured():
            return RedirectResponse("/setup", status_code=302)
        context = {
            "request": request,
            "title": "Add Link",
            "header": "Add Link",
            "action": "/links",
            "link": None,
        }
        context.update(state.instance_context())
        return templates.TemplateResponse("link_form.html", context)

    @app.get("/links/{discord_id}", response_class=HTMLResponse)
    async def edit_link(discord_id: int, request: Request, state: AdminState = Depends(get_state)) -> HTMLResponse:
        if not state.is_configured():
            return RedirectResponse("/setup", status_code=302)
        store = await state.get_link_store()
        mapping = await store.get_mapping(discord_id)
        if mapping is None:
            raise HTTPException(status_code=404, detail="Link not found")
        context = {
            "request": request,
            "title": "Edit Link",
            "header": "Edit Link",
            "action": f"/links/{discord_id}",
            "link": mapping,
        }
        context.update(state.instance_context())
        return templates.TemplateResponse("link_form.html", context)

    @app.post("/links")
    async def create_link(
        discord_id: int = Form(...),
        jellyfin_user_id: str = Form(...),
        instance_name: Optional[str] = Form(None),
        display_name: Optional[str] = Form(None),
        state: AdminState = Depends(get_state),
    ) -> RedirectResponse:
        store = await state.get_link_store()
        jellyfin_clean = jellyfin_user_id.strip()
        if not jellyfin_clean:
            raise HTTPException(status_code=400, detail="jellyfin_user_id is required")
        assert state.config is not None
        resolved_instance = instance_name or state.config.jellyfin.default_instance().name
        if state.config.get_instance(resolved_instance) is None:
            raise HTTPException(status_code=400, detail="Unknown Jellyfin instance")
        mapping = UserMapping(
            discord_id=discord_id,
            jellyfin_user_id=jellyfin_clean,
            display_name=(display_name or "").strip() or None,
            instance_name=resolved_instance,
        )
        await store.upsert_link(mapping)
        return RedirectResponse(f"/?status=created&discord_id={discord_id}", status_code=303)

    @app.post("/links/{discord_id}")
    async def update_link(
        discord_id: int,
        jellyfin_user_id: str = Form(...),
        instance_name: Optional[str] = Form(None),
        display_name: Optional[str] = Form(None),
        state: AdminState = Depends(get_state),
    ) -> RedirectResponse:
        store = await state.get_link_store()
        existing = await store.get_mapping(discord_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Link not found")
        jellyfin_clean = jellyfin_user_id.strip()
        if not jellyfin_clean:
            raise HTTPException(status_code=400, detail="jellyfin_user_id is required")
        assert state.config is not None
        resolved_instance = instance_name or existing.instance_name or state.config.jellyfin.default_instance().name
        if state.config.get_instance(resolved_instance) is None:
            raise HTTPException(status_code=400, detail="Unknown Jellyfin instance")
        mapping = UserMapping(
            discord_id=discord_id,
            jellyfin_user_id=jellyfin_clean,
            display_name=(display_name or "").strip() or None,
            instance_name=resolved_instance,
        )
        await store.upsert_link(mapping)
        return RedirectResponse(f"/?status=updated&discord_id={discord_id}", status_code=303)

    @app.post("/links/{discord_id}/delete")
    async def delete_link(discord_id: int, state: AdminState = Depends(get_state)) -> RedirectResponse:
        store = await state.get_link_store()
        await store.remove_link(discord_id)
        return RedirectResponse("/?status=deleted", status_code=303)

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request, state: AdminState = Depends(get_state)) -> HTMLResponse:
        return templates.TemplateResponse(
            "setup.html",
            {
                "request": request,
                "config_error": state.config_error,
                "env_path": state.env_path,
                "preset": _preset_from_config(state.config),
            },
        )

    @app.post("/setup")
    async def setup_submit(
        discord_token_value: str = Form(...),
        discord_token_env: str = Form("WATCHTIMEBOT_DISCORD_TOKEN"),
        prefix: str = Form("!"),
        activity: Optional[str] = Form(None),
        timezone: str = Form("UTC"),
        watch_window: int = Form(30),
        link_db: str = Form("/state/watchtime_links.db"),
        instance_name: List[str] = Form(...),
        instance_playback_db: List[str] = Form(...),
        instance_server_url: List[Optional[str]] = Form([]),
        instance_api_key_env: List[Optional[str]] = Form([]),
        instance_api_key_value: List[Optional[str]] = Form([]),
        state: AdminState = Depends(get_state),
    ) -> RedirectResponse:
        bundles, env_updates = _collect_instances(
            instance_name,
            instance_playback_db,
            instance_server_url,
            instance_api_key_env,
            instance_api_key_value,
        )
        if not bundles:
            raise HTTPException(status_code=400, detail="At least one Jellyfin instance is required")

        token_value = discord_token_value.strip()
        if not token_value:
            raise HTTPException(status_code=400, detail="Discord token is required")
        env_updates[discord_token_env] = token_value

        state.onboarding.write_config(
            discord_token_env=discord_token_env.strip() or "WATCHTIMEBOT_DISCORD_TOKEN",
            prefix=prefix.strip() or "!",
            activity=(activity or "").strip() or None,
            timezone=timezone.strip() or "UTC",
            default_watch_window_days=max(1, watch_window),
            link_db=link_db.strip() or "watchtime_links.db",
            instances=bundles,
        )
        state.onboarding.update_env(env_updates)
        state.reload_config()
        await state.refresh_link_store()
        return RedirectResponse("/", status_code=303)

    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("watchtimebot.admin_app:app", host="127.0.0.1", port=8000, reload=True)


def _collect_instances(
    names: List[str],
    paths: List[str],
    urls: List[Optional[str]],
    api_envs: List[Optional[str]],
    api_values: List[Optional[str]],
) -> tuple[List[InstanceInput], Dict[str, str]]:
    entries: List[InstanceInput] = []
    env_updates: Dict[str, str] = {}
    for idx, raw_name in enumerate(names):
        name = raw_name.strip()
        if idx >= len(paths):
            continue
        path = paths[idx].strip()
        if not name or not path:
            continue
        server_url = _get_optional(urls, idx)
        api_value = _get_optional(api_values, idx)
        raw_env = _get_optional(api_envs, idx)
        env_name = _normalize_env_name(raw_env, name, bool(api_value))
        if env_name and api_value:
            env_updates[env_name] = api_value
        entries.append(
            InstanceInput(
                name=name,
                playback_db=path,
                server_url=server_url,
                api_key_env=env_name,
            )
        )
    return entries, env_updates


def _get_optional(items: List[Optional[str]], index: int) -> Optional[str]:
    if index >= len(items):
        return None
    value = items[index]
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalize_env_name(candidate: Optional[str], instance_name: str, required: bool) -> Optional[str]:
    if candidate:
        trimmed = candidate.strip()
        return trimmed or None
    if not required:
        return None
    slug = "".join(ch if ch.isalnum() else "_" for ch in instance_name.upper()).strip("_")
    slug = slug or "INSTANCE"
    return f"WATCHTIMEBOT_JELLYFIN_{slug}_API_KEY"
