"""Support for HP iLO sensors with unified attributes."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Any

from datetime import datetime
from homeassistant.components.sensor import RestoreSensor
from homeassistant.core import callback
from homeassistant.const import UnitOfEnergy

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    UnitOfTemperature,
    UnitOfPower,
    PERCENTAGE,
    CONF_HOST,
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_PORT,
)
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, format_mac
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, DEFAULT_PORT
from .coordinator import HpIloCoordinator
import logging
_LOGGER = logging.getLogger(__name__)

def safe_get(data, *keys):
    """Safely traverse nested dictionaries."""
    for key in keys:
        try:
            data = data[key]
        except (KeyError, TypeError, IndexError):
            return None
    return data

@dataclass
class HpIloSensorEntityDescription(SensorEntityDescription):
    """Description for iLO sensors."""
    value_fn: Callable[[dict], Any] = None
    attr_fn: Callable[[dict], dict[str, Any]] = None

class HpIloEnergySensor(RestoreSensor, CoordinatorEntity):
    """Energy sensor that integrates power readings using Left Riemann sum."""
    
    _attr_has_entity_name = True
    
    def __init__(self, coordinator, serial, device_info):
        """Initialize the energy sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{serial}_energy_total"
        self._attr_name = "Energy Usage"
        self._attr_device_info = device_info
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_suggested_display_precision = 3
        self._attr_icon = "mdi:lightning-bolt"
        
        self._total_energy = 0.0
        self._last_power = None
        self._last_update = None

        if coordinator.data:
            power_data = safe_get(coordinator.data, "power_readings", "present_power_reading")
            if power_data is not None:
                self._last_power = power_data[0] if isinstance(power_data, tuple) else power_data
                self._last_update = datetime.now()
    
    async def async_added_to_hass(self):
        """Restore previous energy total from state."""
        await super().async_added_to_hass()
        
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._total_energy = float(last_state.state)
            except (ValueError, TypeError):
                self._total_energy = 0.0
    
    @property
    def native_value(self):
        """Return the current total energy in kWh."""
        return round(self._total_energy, 3)
    
    @property
    def extra_state_attributes(self):
        """Return additional state attributes."""
        return {
            "last_power_reading": self._last_power,
            "last_update": self._last_update.isoformat() if self._last_update else None,
            "integration_method": "left_riemann",
        }
    
    @callback
    def _handle_coordinator_update(self):
        """Calculate energy increment using Left Riemann sum."""
        
        _LOGGER.debug(f"ENERGY DEBUG: Coordinator update triggered at {datetime.now()}")
        
        power_data = safe_get(
            self.coordinator.data, 
            "power_readings", 
            "present_power_reading"
        )
        # Extract numeric value if tuple
        current_power = power_data[0] if isinstance(power_data, tuple) else power_data
        
        _LOGGER.debug(f"ENERGY DEBUG: Current power = {current_power}, Last power = {self._last_power}")
        
        if current_power is None:
            _LOGGER.debug("ENERGY DEBUG: No power reading, returning")
            return
        
        now = datetime.now()
        
        if self._last_power is not None and self._last_update is not None:
            time_delta_hours = (now - self._last_update).total_seconds() / 3600
            energy_increment_wh = self._last_power * time_delta_hours
            energy_increment_kwh = energy_increment_wh / 1000
            self._total_energy += energy_increment_kwh
            
            _LOGGER.debug(
                f"ENERGY DEBUG: Calculated {energy_increment_kwh:.6f} kWh "
                f"({self._last_power}W × {time_delta_hours:.4f}h). Total: {self._total_energy:.6f} kWh"
            )
        else:
            _LOGGER.debug("ENERGY DEBUG: First reading, initializing")
        
        self._last_power = current_power
        self._last_update = now
        self.async_write_ha_state()

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up sensors based on dynamic model."""
    coordinator = HpIloCoordinator(
        hass,
        entry.data[CONF_HOST],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        entry.data.get(CONF_PORT, DEFAULT_PORT),
    )

    await coordinator.async_config_entry_first_refresh()

    if not coordinator.last_update_success:
        raise ConfigEntryNotReady(f"Unable to connect to iLO at {entry.data[CONF_HOST]}")

    # --- 1. EXTRACT STABLE HARDWARE IDENTIFIERS ---
    data = coordinator.data
    host_data = data.get("host_data", [])
    
    # Extract hardware identifiers - CRITICAL for stable unique IDs
    serial = None
    uuid = None
    mac_address = None
    prod_name = "ProLiant"
    
    for item in host_data:
        if not isinstance(item, dict):
            continue
            
        if item.get("Product Name"):
            prod_name = item["Product Name"]
            
        # Serial number is PRIMARY identifier
        found_serial = item.get("Serial Number") or item.get("Serial")
        if found_serial and found_serial != "Unknown":
            serial = found_serial
            
        # UUID is SECONDARY identifier
        found_uuid = item.get("cUUID") or item.get("UUID")
        if found_uuid and found_uuid != "Unknown":
            uuid = found_uuid
        
        # Extract iLO MAC address (for device connections)
        if item.get("MAC") and item.get("Port") == "iLO":
            mac_address = format_mac(item["MAC"])
    
    # Validate we have at least one stable identifier
    if not serial and not uuid:
        _LOGGER.error(
            f"Cannot extract stable hardware identifier (serial or UUID) from {entry.data[CONF_HOST]}. "
            "Entity unique IDs will not be stable across config entry changes."
        )
        # Fallback to entry_id only as last resort
        hardware_id = entry.entry_id
    else:
        # Use serial as primary, UUID as fallback
        hardware_id = serial or uuid
    
    _LOGGER.info(f"Using hardware identifier: {hardware_id} (serial={serial}, uuid={uuid})")
    
    # Extract hardware version (System ROM/BIOS)
    hw_version = None
    firmware_info = safe_get(data, "health", "firmware_information")
    if firmware_info:
        system_rom = firmware_info.get("System ROM")
        if system_rom:
            hw_version = system_rom.strip()
    
    # Build connections - use formatted MAC
    connections = set()
    if mac_address:
        connections.add((CONNECTION_NETWORK_MAC, mac_address))

    # --- 2. BUILD STABLE DEVICE INFO ---
    # CRITICAL: Use hardware identifiers for device registry
    device_identifiers = {(DOMAIN, hardware_id)}
    
    # Add additional identifiers for flexibility (helps with migration)
    if serial and serial != hardware_id:
        device_identifiers.add((DOMAIN, serial))
    if uuid and uuid != hardware_id:
        device_identifiers.add((DOMAIN, uuid))
    
    device_info = DeviceInfo(
        identifiers=device_identifiers,
        connections=connections if connections else None,
        name=prod_name,
        manufacturer="HPE",
        model=prod_name.replace("ProLiant ", ""),
        serial_number=serial,
        hw_version=hw_version,
        sw_version=safe_get(data, "fw_version"),
        configuration_url=f"https://{entry.data[CONF_HOST]}",
    )

    entities = []

    # --- 3. POWER STATE ---
    entities.append(HpIloSensor(coordinator, HpIloSensorEntityDescription(
        key="power_state_master",
        name="Power State",
        device_class=SensorDeviceClass.ENUM,
        options=["ON", "OFF", "Unknown"],
        value_fn=lambda d: d.get("power_status", "Unknown"),
    ), hardware_id, device_info))

    # --- 4. FIRMWARE & LICENSE ---
    entities.append(HpIloSensor(coordinator, HpIloSensorEntityDescription(
        key="ilo_details",
        name="iLO System Details",
        icon="mdi:information-outline",
        value_fn=lambda d: safe_get(d, "fw_version", "firmware_version") or "Ready",
        attr_fn=lambda d: {
            "firmware_date": safe_get(d, "fw_version", "firmware_date"),
            "license_type": safe_get(d, "fw_version", "license_type"),
            "system_rom": safe_get(d, "health", "firmware_information", "System ROM"),
            "uuid": uuid,
        }
    ), hardware_id, device_info))

    # --- 5. HEALTH SUMMARY ---
    health_glance = safe_get(data, "health", "health_at_a_glance")
    if health_glance:
        entities.append(HpIloSensor(coordinator, HpIloSensorEntityDescription(
            key="health_summary",
            name="System Health Summary",
            icon="mdi:server-network",
            value_fn=lambda d: "OK" if all(v.get("status") in ["OK", "Not Installed"] for v in health_glance.values()) else "Degraded",
            attr_fn=lambda d: {
                **{k: v.get("status") for k, v in safe_get(d, "health", "health_at_a_glance").items()},
                "psu_redundancy": safe_get(d, "health", "health_at_a_glance", "power_supplies", "redundancy")
            }
        ), hardware_id, device_info))

    # --- 6. POWER ANALYSIS (Real-time Watts) ---
    pwr = safe_get(data, "power_readings")
    if pwr:
        entities.append(HpIloSensor(coordinator, HpIloSensorEntityDescription(
            key="power_readings_detailed",
            name="Power Analysis",
            device_class=SensorDeviceClass.POWER,
            native_unit_of_measurement=UnitOfPower.WATT,
            state_class=SensorStateClass.MEASUREMENT,
            value_fn=lambda d: safe_get(d, "power_readings", "present_power_reading"),
            attr_fn=lambda d: {
                "min": safe_get(d, "power_readings", "minimum_power_reading"),
                "max": safe_get(d, "power_readings", "maximum_power_reading"),
                "avg": safe_get(d, "power_readings", "average_power_reading"),
            }
        ), hardware_id, device_info))

    # --- 7. TEMPERATURES & FANS ---
    temps = safe_get(data, "health", "temperature")
    if temps:
        for label, s_data in temps.items():
            if s_data.get("status") == "Not Installed" or s_data.get("currentreading") == "N/A": 
                continue
            entities.append(HpIloSensor(coordinator, HpIloSensorEntityDescription(
                key=f"temp_{label}",
                name=label,
                native_unit_of_measurement=UnitOfTemperature.CELSIUS,
                device_class=SensorDeviceClass.TEMPERATURE,
                state_class=SensorStateClass.MEASUREMENT,
                value_fn=lambda d, l=label: safe_get(d, "health", "temperature", l, "currentreading", 0)
            ), hardware_id, device_info))

    fans = safe_get(data, "health", "fans")
    if fans:
        for label, s_data in fans.items():
            if s_data.get("status") == "Not Installed": 
                continue
            entities.append(HpIloSensor(coordinator, HpIloSensorEntityDescription(
                key=f"fan_{label.lower().replace(' ', '_')}",
                name=label,
                native_unit_of_measurement=PERCENTAGE,
                icon="mdi:fan",
                state_class=SensorStateClass.MEASUREMENT,
                value_fn=lambda d, l=label: safe_get(d, "health", "fans", l, "speed", 0),
                attr_fn=lambda d, l=label: {
                    "status": safe_get(d, "health", "fans", l, "status"), 
                    "zone": safe_get(d, "health", "fans", l, "zone")
                }
            ), hardware_id, device_info))

    # --- 8. MEMORY ---
    mem_sum = safe_get(data, "health", "memory", "memory_details_summary", "cpu_1")
    if mem_sum:
        entities.append(HpIloSensor(coordinator, HpIloSensorEntityDescription(
            key="memory_detailed",
            name="Memory Inventory",
            icon="mdi:memory",
            value_fn=lambda d: safe_get(d, "health", "memory", "memory_details_summary", "cpu_1", "total_memory_size"),
            attr_fn=lambda d: {
                "amp_mode": safe_get(d, "health", "memory", "advanced_memory_protection", "amp_mode_status"),
                "sockets": safe_get(d, "health", "memory", "memory_details_summary", "cpu_1", "number_of_sockets"),
                "frequency": safe_get(d, "health", "memory", "memory_details_summary", "cpu_1", "operating_frequency"),
            }
        ), hardware_id, device_info))

    # --- 9. NETWORK ---
    nic_ip = safe_get(data, "health", "nic_information", "Embedded", "ip_address")
    if nic_ip:
        entities.append(HpIloSensor(coordinator, HpIloSensorEntityDescription(
            key="nic_status",
            name="Network Inventory",
            icon="mdi:lan",
            value_fn=lambda d: "Connected",
            attr_fn=lambda d: safe_get(d, "health", "nic_information", "Embedded")
        ), hardware_id, device_info))

    # --- 10. ENERGY USAGE (For Energy Dashboard) ---
    if safe_get(data, "power_readings", "present_power_reading") is not None:
        entities.append(HpIloEnergySensor(coordinator, hardware_id, device_info))

    async_add_entities(entities)

class HpIloSensor(CoordinatorEntity, SensorEntity):
    """Generic iLO Sensor with stable hardware-based unique IDs."""
    entity_description: HpIloSensorEntityDescription

    def __init__(self, coordinator, description, hardware_id, device_info):
        """Initialize sensor with hardware-based unique ID."""
        super().__init__(coordinator)
        self.entity_description = description
        # CRITICAL: Use hardware_id (serial/UUID) instead of entry_id
        self._attr_unique_id = f"{hardware_id}_{description.key}"
        self._attr_device_info = device_info

    @property
    def native_value(self):
        try:
            val = self.entity_description.value_fn(self.coordinator.data)
            return val[0] if isinstance(val, tuple) else val
        except Exception: 
            return None

    @property
    def extra_state_attributes(self):
        if not self.entity_description.attr_fn: 
            return None
        try:
            attrs = self.entity_description.attr_fn(self.coordinator.data)
            return {k: (v[0] if isinstance(v, tuple) else v) for k, v in attrs.items() if v is not None}
        except Exception: 
            return None