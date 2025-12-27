"""DataUpdateCoordinator for HP iLO."""
from datetime import timedelta
import logging
import hpilo

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .const import DOMAIN, DEFAULT_PORT

_LOGGER = logging.getLogger(__name__)

class HpIloCoordinator(DataUpdateCoordinator):
    """Manages polling the iLO device."""

    def __init__(self, hass, host, login, password, port):
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{host}",
            update_interval=timedelta(seconds=60),
        )
        self.host = host
        self.login = login
        self.password = password
        # Force 443 if 80 is passed, as RIBCL requires SSL
        self.port = int(port) if int(port) != 80 else 443
        self.ilo = None

    async def _async_update_data(self):
        """Fetch data from iLO."""
        return await self.hass.async_add_executor_job(self._get_ilo_data)

    def _get_ilo_data(self):
        """Synchronous fetcher."""
        if not self.ilo:
            self.ilo = hpilo.Ilo(
                hostname=self.host,
                login=self.login,
                password=self.password,
                port=self.port,
            )

        data = {}
        try:
            # 1. Get Power State
            data["power_status"] = self.ilo.get_host_power_status()
            
            # 2. Get Health (Temps/Fans)
            data["health"] = self.ilo.get_embedded_health()
            
            # 3. Get Power Readings
            data["power_readings"] = self.ilo.get_power_readings()
            
            # 4. Get Server Name
            data["server_name"] = self.ilo.get_server_name()

            # 5. Get Host Data (Hardware Info)
            data["host_data"] = self.ilo.get_host_data()
            
            # 6. Get Firmware Version
            fw_info = self.ilo.get_fw_version()
            data["fw_version"] = fw_info.get("firmware_version") if isinstance(fw_info, dict) else fw_info

            # 7. Get Firmware Information
            # data["firmware_information"] = self.ilo.get_fw_version()
            
            
        except Exception as err:
            raise UpdateFailed(f"Error communicating with iLO: {err}")

        return data