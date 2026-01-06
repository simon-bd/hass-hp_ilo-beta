"""The HP iLO Integration."""
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import Platform, CONF_HOST, CONF_USERNAME, CONF_PASSWORD, CONF_PORT
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, DEFAULT_PORT
from .coordinator import HpIloCoordinator

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up HP iLO config entry.

    Initialize the coordinator and perform the first refresh here so that
    any connection errors raise ConfigEntryNotReady from the integration
    setup (before platforms are forwarded).
    """
    hass.data.setdefault(DOMAIN, {})

    coordinator = HpIloCoordinator(
        hass,
        entry.data[CONF_HOST],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        entry.data.get(CONF_PORT, DEFAULT_PORT),
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as err:
        raise ConfigEntryNotReady from err

    # If the first refresh didn't succeed, treat as not ready so HA retries setup
    if not coordinator.last_update_success:
        raise ConfigEntryNotReady(f"Unable to connect to iLO at {entry.data[CONF_HOST]}")

    hass.data[DOMAIN][entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN):
            hass.data.pop(DOMAIN, None)
    return unloaded