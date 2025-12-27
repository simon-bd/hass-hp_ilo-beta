"""Config flow for HP iLO."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
import hpilo

from homeassistant import config_entries
from homeassistant.components import ssdp
from homeassistant.const import CONF_HOST, CONF_USERNAME, CONF_PASSWORD, CONF_PORT, CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, DEFAULT_PORT


class HpIloFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HP iLO."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovered_host: str | None = None
        self._discovered_name: str | None = None
        self._discovered_description: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Normalize port to integer
            user_input[CONF_PORT] = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            
            # Create unique ID from host:port combination
            # This prevents adding the same iLO device multiple times
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            unique_id = f"{host}:{port}"
            
            await self.async_set_unique_id(unique_id)
            # Abort if this exact host:port is already configured
            # Updates the existing entry with new data if needed
            self._abort_if_unique_id_configured(
                updates={
                    CONF_HOST: host,
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_PORT: port,
                }
            )

            # Test connection with detailed error handling
            result = await self.hass.async_add_executor_job(
                self._test_connection,
                user_input[CONF_HOST],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                user_input[CONF_PORT],
            )

            if result["success"]:
                # Build a descriptive title with priority:
                # 1. Product name (e.g., "ProLiant DL20 Gen9")
                # 2. Server name if meaningful
                # 3. Fallback to "iLO {host}"
                product_name = result.get("product_name")
                server_name = result.get("server_name")
                
                if product_name:
                    # Use product name, optionally with server name
                    if server_name and server_name.lower() not in [host.lower(), "localhost", "unknown"]:
                        title = f"{product_name} ({server_name})"
                    else:
                        title = product_name
                elif server_name and server_name.lower() not in [host.lower(), "localhost", "unknown"]:
                    title = server_name
                else:
                    title = f"iLO {host}"
                
                return self.async_create_entry(
                    title=title,
                    data=user_input,
                )

            errors["base"] = result["error"]

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_USERNAME, default="Administrator"): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
                }
            ),
            errors=errors,
        )

    async def async_step_ssdp(
        self, discovery_info: ssdp.SsdpServiceInfo
    ) -> FlowResult:
        """Handle SSDP discovery."""
        # Extract host from SSDP location URL
        host = discovery_info.ssdp_location
        if host:
            # Parse hostname from URL like http://192.168.1.100:49152/
            host = host.split("://")[1].split(":")[0] if "://" in host else host.split(":")[0]
        
        # Extract friendly name and model
        name = discovery_info.upnp.get(ssdp.ATTR_UPNP_FRIENDLY_NAME, "HP iLO")
        model = discovery_info.upnp.get(ssdp.ATTR_UPNP_MODEL_NAME, "")
        
        # Create unique ID from discovered host
        unique_id = f"{host}:{DEFAULT_PORT}"
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})

        # Store discovery info for later steps
        self._discovered_host = host
        self._discovered_name = name
        self._discovered_description = model

        # Show confirmation form
        self.context["title_placeholders"] = {
            "name": name,
            "host": host,
            "description": model,
        }
        
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle user confirmation of discovered device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Test connection with provided credentials
            result = await self.hass.async_add_executor_job(
                self._test_connection,
                self._discovered_host,
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                DEFAULT_PORT,
            )

            if result["success"]:
                # Use discovered name or build from server name
                server_name = result.get("server_name")
                
                if self._discovered_name and self._discovered_name != self._discovered_host:
                    title = self._discovered_name
                elif server_name and server_name.lower() not in [self._discovered_host.lower(), "localhost", "unknown"]:
                    title = server_name
                else:
                    title = f"iLO {self._discovered_host}"
                
                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_HOST: self._discovered_host,
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_PORT: DEFAULT_PORT,
                    },
                )

            errors["base"] = result["error"]

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME, default="Administrator"): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            description_placeholders={
                "name": self._discovered_name,
                "host": self._discovered_host,
                "description": self._discovered_description or "HP iLO",
            },
            errors=errors,
        )

    def _test_connection(
        self, host: str, user: str, password: str, port: int
    ) -> dict[str, Any]:
        """Test connection to HP iLO and return detailed error info.
        
        Returns:
            Dictionary with keys:
                - success: bool indicating if connection succeeded
                - error: str error code if failed (None if success)
                - server_name: str server name if connection succeeded
                - product_name: str hardware model if available
        """
        try:
            # Auto-correct port 80 to 443 (RIBCL requires SSL)
            if port == 80:
                port = 443
                
            ilo = hpilo.Ilo(host, user, password, port=port)
            
            # Get server name and try to get product name
            server_name = ilo.get_server_name()
            product_name = None
            
            try:
                # Try to get hardware model for better naming
                host_data = ilo.get_host_data()
                for item in host_data:
                    if isinstance(item, dict) and item.get("Product Name"):
                        product_name = item["Product Name"]
                        break
            except Exception:
                pass  # Product name is optional
            
            return {
                "success": True,
                "error": None,
                "server_name": server_name,
                "product_name": product_name,
            }
            
        except hpilo.IloLoginFailed as err:
            # Authentication failure - invalid username or password
            return {
                "success": False,
                "error": "invalid_auth",
                "server_name": None,
                "product_name": None,
            }
            
        except hpilo.IloCommunicationError as err:
            # Network/connection issues - device unreachable, wrong port, timeout
            return {
                "success": False,
                "error": "cannot_connect",
                "server_name": None,
                "product_name": None,
            }
            
        except hpilo.IloError as err:
            # Generic iLO error - firmware issue, unsupported operation, etc.
            return {
                "success": False,
                "error": "unknown",
                "server_name": None,
                "product_name": None,
            }
            
        except Exception as err:
            # Catch-all for unexpected errors
            return {
                "success": False,
                "error": "unknown",
                "server_name": None,
                "product_name": None,
            }