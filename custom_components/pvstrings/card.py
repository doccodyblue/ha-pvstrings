"""Serve the bundled dashboard card and register it as a Lovelace resource.

The card (``www/pvstrings-dash.js``) is vendored from ha-pvstrings-dash at a
release tag by ``tools/sync_dash.sh``; it is not edited here.  Shipping it with
the integration is HACS policy for a card that only works with one
integration, and it means the two cannot drift apart in version.

A resource that already loads a copy of the card -- the old separate HACS
plugin, a file in ``/config/www``, a development server -- is left alone and
wins: two copies would race for the same custom element names, and the first
one to load defines them.  A repair issue asks the user to remove it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_FILE = "pvstrings-dash.js"
#: ``PVS_VERSION`` of the vendored file; doubles as the cache buster.  A test
#: holds the two together.
CARD_VERSION = "0.22.1"
URL_BASE = f"/{DOMAIN}_static"
CARD_URL = f"{URL_BASE}/{CARD_FILE}"
ISSUE_SEPARATE_CARD = "separate_dashboard_card"

_DATA_REGISTERED = f"{DOMAIN}_card_registered"


def card_resource_url() -> str:
    return f"{CARD_URL}?v={CARD_VERSION}"


def is_own(url: str) -> bool:
    return url.split("?", 1)[0] == CARD_URL


def is_foreign_copy(url: str) -> bool:
    """A resource loading some other copy of the card."""
    return CARD_FILE in url.split("?", 1)[0] and not is_own(url)


async def async_register_card(hass: HomeAssistant) -> None:
    """Once per Home Assistant run; later entries find it done."""
    if hass.data.get(_DATA_REGISTERED):
        return
    hass.data[_DATA_REGISTERED] = True

    await hass.http.async_register_static_paths(
        [StaticPathConfig(URL_BASE, str(Path(__file__).parent / "www"), True)]
    )
    url = card_resource_url()

    lovelace = hass.data.get("lovelace")
    resources = getattr(lovelace, "resources", None)
    if resources is None:
        add_extra_js_url(hass, url)
        return
    if hasattr(resources, "async_get_info"):
        await resources.async_get_info()  # storage mode loads lazily
    items = list(resources.async_items() or [])

    foreign = [item for item in items if is_foreign_copy(item.get("url", ""))]
    own = [item for item in items if is_own(item.get("url", ""))]
    storage = hasattr(resources, "async_create_item")

    if foreign:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_SEPARATE_CARD,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_SEPARATE_CARD,
            translation_placeholders={"url": foreign[0]["url"]},
        )
        # Ours alongside it would race it.  Only an item we created is removed.
        if storage:
            for item in own:
                await resources.async_delete_item(item["id"])
        return
    ir.async_delete_issue(hass, DOMAIN, ISSUE_SEPARATE_CARD)

    if not storage:
        # YAML resources are the user's file; load the card without them.
        add_extra_js_url(hass, url)
        return
    if not own:
        await resources.async_create_item({"res_type": "module", "url": url})
        _LOGGER.info("pvstrings: registered dashboard card %s", url)
        return
    first, *rest = own
    if first.get("url") != url:
        await resources.async_update_item(first["id"], {"url": url})
    for item in rest:
        await resources.async_delete_item(item["id"])


async def async_unregister_card(hass: HomeAssistant) -> None:
    """Remove the resource we created; called when the last entry goes."""
    ir.async_delete_issue(hass, DOMAIN, ISSUE_SEPARATE_CARD)
    resources = getattr(hass.data.get("lovelace"), "resources", None)
    if resources is None or not hasattr(resources, "async_delete_item"):
        return
    if hasattr(resources, "async_get_info"):
        await resources.async_get_info()
    for item in list(resources.async_items() or []):
        if is_own(item.get("url", "")):
            await resources.async_delete_item(item["id"])
