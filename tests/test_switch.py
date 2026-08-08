# Copyright (c) 2026 Kenneth Baker <bakerkj@umich.edu>
# All rights reserved.
"""Switch platform tests — specifically, which device the switch lands on.

The switch must attach to the *VTherm's own* device so HA names it
"<VTherm name> Progressive Fan" and shows it alongside the thermostat's
entities. It has to do that by pointing at the device (``device_entry``)
rather than describing it (``device_info``): from HA 2026.8 a ``device_info``
lookup only matches devices owned by our own config entry, so describing
VTherm's device would miss it and silently create a nameless duplicate.

The registry assertions go through a real ``EntityPlatform`` rather than a
stub callback — HA converts ``device_info`` into a device during
``async_add_entities``, so a stubbed callback would never run the find-or-create
this is guarding and the test would pass no matter what.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockEntityPlatform,
)

from custom_components.vtherm_progressive_fan.const import (
    CONF_VTHERM_ENTITY_ID,
    DOMAIN,
)
from custom_components.vtherm_progressive_fan.switch import async_setup_entry

VTHERM_DOMAIN = "versatile_thermostat"
VTHERM_ENTITY_ID = "climate.first_floor_versatile"


@pytest.fixture
def vtherm_device(hass: HomeAssistant) -> dr.DeviceEntry:
    """A device owned by versatile_thermostat, with its climate entity on it."""
    vtherm_entry = MockConfigEntry(domain=VTHERM_DOMAIN, title="First Floor Versatile")
    vtherm_entry.add_to_hass(hass)

    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=vtherm_entry.entry_id,
        identifiers={(VTHERM_DOMAIN, vtherm_entry.entry_id)},
        name="First Floor Versatile",
    )
    er.async_get(hass).async_get_or_create(
        "climate",
        VTHERM_DOMAIN,
        "first-floor-unique-id",
        config_entry=vtherm_entry,
        device_id=device.id,
        suggested_object_id="first_floor_versatile",
    )
    return device


def _our_entry(hass: HomeAssistant, vtherm_entity_id: str | None) -> MockConfigEntry:
    """A Progressive Fan config entry linked to ``vtherm_entity_id``."""
    data: dict[str, Any] = {}
    if vtherm_entity_id is not None:
        data[CONF_VTHERM_ENTITY_ID] = vtherm_entity_id
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    # The switch only calls set_enabled() on its plugin.
    entry.runtime_data = SimpleNamespace(
        plugin=SimpleNamespace(set_enabled=lambda _: None)
    )
    return entry


async def _build(hass: HomeAssistant, entry: MockConfigEntry) -> list[Any]:
    """Run the platform's setup and return the entities it built."""
    added: list[Any] = []

    def _add(new_entities, update_before_add=False):
        added.extend(new_entities)

    await async_setup_entry(hass, entry, _add)
    return added


async def _register(
    hass: HomeAssistant, entry: MockConfigEntry, entities: list[Any]
) -> None:
    """Add entities through a real EntityPlatform bound to our config entry.

    This is the step that turns ``device_info`` into a device registry entry.
    """
    platform = MockEntityPlatform(hass, domain="switch", platform_name=DOMAIN)
    platform.config_entry = entry
    await platform.async_add_entities(entities)


async def test_switch_attaches_to_the_vtherm_device(
    hass: HomeAssistant, vtherm_device: dr.DeviceEntry
) -> None:
    """The switch points at VTherm's existing device."""
    added = await _build(hass, _our_entry(hass, VTHERM_ENTITY_ID))

    assert len(added) == 1
    assert added[0].device_entry is not None
    assert added[0].device_entry.id == vtherm_device.id


async def test_switch_does_not_describe_a_device(
    hass: HomeAssistant, vtherm_device: dr.DeviceEntry
) -> None:
    """No ``device_info``, so HA never runs a find-or-create."""
    added = await _build(hass, _our_entry(hass, VTHERM_ENTITY_ID))

    assert added[0].device_info is None


async def test_registering_the_switch_creates_no_extra_device(
    hass: HomeAssistant, vtherm_device: dr.DeviceEntry
) -> None:
    """Registering the switch leaves the device registry untouched.

    Fails on HA 2026.8 without the fix: the ``device_info`` lookup is scoped to
    our own config entry, misses VTherm's device, and creates a duplicate.
    """
    entry = _our_entry(hass, VTHERM_ENTITY_ID)
    before = set(dr.async_get(hass).devices)

    await _register(hass, entry, await _build(hass, entry))

    assert set(dr.async_get(hass).devices) == before


async def test_registered_switch_lands_on_the_vtherm_device(
    hass: HomeAssistant, vtherm_device: dr.DeviceEntry
) -> None:
    """After registration the switch's registry entry names VTherm's device."""
    entry = _our_entry(hass, VTHERM_ENTITY_ID)
    added = await _build(hass, entry)

    await _register(hass, entry, added)

    reg_entry = er.async_get(hass).async_get(added[0].entity_id)
    assert reg_entry is not None
    assert reg_entry.device_id == vtherm_device.id


async def test_unlinked_switch_has_no_device(hass: HomeAssistant) -> None:
    """With no VTherm entity configured, the switch attaches to nothing."""
    added = await _build(hass, _our_entry(hass, None))

    assert len(added) == 1
    assert added[0].device_entry is None


async def test_unknown_vtherm_entity_has_no_device(hass: HomeAssistant) -> None:
    """A configured but unregistered entity resolves to no device, not a crash."""
    added = await _build(hass, _our_entry(hass, "climate.does_not_exist"))

    assert len(added) == 1
    assert added[0].device_entry is None
