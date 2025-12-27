"""DataUpdateCoordinator for HP iLO."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

import hpilo

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN, DEFAULT_PORT, DEFAULT_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


class HpIloCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manages polling the iLO device.
    
    This coordinator handles all communication with HP iLO devices via the
    python-hpilo library. It polls the device at a configurable interval and
    provides data to all sensor entities.
    
    Port Handling:
    - Default port is 443 (HTTPS/SSL required for RIBCL protocol)
    - If port 80 is explicitly configured, it's automatically converted to 443
    - iLO firmware requires SSL/TLS for RIBCL communication
    """

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        login: str,
        password: str,
        port: int = DEFAULT_PORT,
        update_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        """Initialize the coordinator.
        
        Args:
            hass: Home Assistant instance
            host: iLO hostname or IP address
            login: iLO username
            password: iLO password
            port: iLO port (default 443, auto-corrects 80 -> 443)
            update_interval: Polling interval in seconds
        """
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{host}",
            update_interval=timedelta(seconds=update_interval),
        )
        self.host = host
        self.login = login
        self.password = password
        
        # Port validation and auto-correction
        # RIBCL protocol requires SSL (port 443), so if user configures port 80
        # (legacy HTTP), automatically upgrade to 443 for compatibility
        port_int = int(port)
        if port_int == 80:
            _LOGGER.warning(
                "Port 80 configured for %s, but iLO RIBCL requires SSL. "
                "Auto-correcting to port 443.",
                host
            )
            self.port = 443
        else:
            self.port = port_int
        
        self.ilo: hpilo.Ilo | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from iLO.
        
        Returns:
            Dictionary containing all iLO data (power status, health, etc.)
            
        Raises:
            UpdateFailed: If communication with iLO fails
        """
        return await self.hass.async_add_executor_job(self._get_ilo_data)

    def _get_ilo_data(self) -> dict[str, Any]:
        """Synchronous data fetcher (runs in executor).
        
        This method performs all iLO API calls synchronously in a thread pool
        to avoid blocking the event loop.
        
        Returns:
            Dictionary with keys:
                - power_status: Current power state (ON/OFF)
                - health: Embedded health data (temps, fans, memory, etc.)
                - power_readings: Real-time power consumption data
                - server_name: Server hostname
                - host_data: Hardware inventory (serial, UUID, NICs)
                - fw_version: Firmware version information
                
        Raises:
            UpdateFailed: For authentication, connection, or API errors
        """
        # Lazy initialization of iLO connection
        if not self.ilo:
            try:
                self.ilo = hpilo.Ilo(
                    hostname=self.host,
                    login=self.login,
                    password=self.password,
                    port=self.port,
                )
            except Exception as err:
                raise UpdateFailed(
                    f"Failed to initialize iLO connection to {self.host}:{self.port}: {err}"
                ) from err

        data: dict[str, Any] = {}
        
        try:
            # Fetch all iLO data in sequence
            # Each call can take 1-3 seconds, total ~10-15s for full refresh
            
            # 1. Power state (ON/OFF/RESET)
            data["power_status"] = self.ilo.get_host_power_status()
            
            # 2. Embedded health (temperatures, fans, memory, PSUs, storage)
            data["health"] = self.ilo.get_embedded_health()
            
            # 3. Power readings (current/min/max/avg watts)
            data["power_readings"] = self.ilo.get_power_readings()
            
            # 4. Server name (hostname from iLO perspective)
            data["server_name"] = self.ilo.get_server_name()
            
            # 5. Hardware inventory (serial, UUID, MAC addresses, product name)
            data["host_data"] = self.ilo.get_host_data()
            
            # 6. Firmware version and license info
            fw_info = self.ilo.get_fw_version()
            # Handle both dict and string returns from different iLO versions
            data["fw_version"] = (
                fw_info.get("firmware_version") if isinstance(fw_info, dict) else fw_info
            )
            
        except hpilo.IloLoginFailed as err:
            # Authentication failure - likely bad credentials
            raise UpdateFailed(
                f"Authentication failed for iLO at {self.host}: {err}"
            ) from err
            
        except hpilo.IloCommunicationError as err:
            # Network/connection issues - device unreachable or timeout
            raise UpdateFailed(
                f"Communication error with iLO at {self.host}: {err}"
            ) from err
            
        except hpilo.IloError as err:
            # Generic iLO error - API/firmware issue
            raise UpdateFailed(
                f"iLO API error from {self.host}: {err}"
            ) from err
            
        except Exception as err:
            # Catch-all for unexpected errors
            _LOGGER.exception("Unexpected error fetching iLO data from %s", self.host)
            raise UpdateFailed(
                f"Unexpected error communicating with iLO at {self.host}: {err}"
            ) from err

        return data