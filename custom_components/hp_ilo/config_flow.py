
"""Config flow for HP iLO."""
from __future__ import annotations

from typing import Any, Optional
import urllib.parse
import xml.etree.ElementTree as ET

import voluptuous as vol
import aiohttp
import async_timeout
import hpilo
import logging
_LOGGER = logging.getLogger(__name__)


from homeassistant import config_entries
from homeassistant.components import ssdp
from homeassistant.const import CONF_HOST, CONF_USERNAME, CONF_PASSWORD, CONF_PORT
from homeassistant.core import callback, HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, DEFAULT_PORT


XML_ALL_PATH = "/xmldata?item=All"
BASICDEVICE_PATH = "/upnp/BasicDevice.xml"
REDFISH_ROOT = "/redfish/v1/"


class HpIloFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovered_host: str | None = None
        self._discovered_name: str | None = None
        self._discovered_description: str | None = None
        self._discovered_mac: str | None = None

    # --------------------------
    # Helpers
    # --------------------------
    @staticmethod
    def _parse_host_from_location(location: str | None) -> str | None:
        """Return hostname from SSDP location URL (handles IPv4/IPv6 with ports)."""
        if not location:
            return None
        try:
            parsed = urllib.parse.urlparse(location)
            if parsed.hostname:
                return parsed.hostname
            host = location
            if "://" in host:
                host = host.split("://", 1)[1]
            host = host.split("/", 1)[0]
            if host.startswith("["):  # [fe80::1]:49152
                return host[1:].split("]", 1)[0]
            return host.split(":", 1)[0]
        except Exception:
            return None

    @staticmethod
    async def _fetch_text(
        session: aiohttp.ClientSession, url: str, *, verify_ssl: bool, timeout_s: float = 5.0
    ) -> str | None:
        try:
            async with async_timeout.timeout(timeout_s):
                async with session.get(url, ssl=verify_ssl) as resp:
                    if resp.status < 300:
                        return await resp.text()
                    return None
        except Exception:
            return None

    @staticmethod
    def _parse_basicdevice(xml_text: str | None) -> dict[str, str]:
        """Parse UPnP BasicDevice.xml for friendlyName/model/manufacturer."""
        out = {"friendlyName": "", "modelName": "", "manufacturer": ""}
        if not xml_text:
            return out
        try:
            root = ET.fromstring(xml_text)
            ns = {"u": "urn:schemas-upnp-org:device-1-0"}
            out["friendlyName"] = root.findtext(".//u:friendlyName", default="", namespaces=ns) or ""
            out["modelName"] = root.findtext(".//u:modelName", default="", namespaces=ns) or ""
            out["manufacturer"] = root.findtext(".//u:manufacturer", default="", namespaces=ns) or ""
        except Exception:
            pass
        return out

    @staticmethod
    def _parse_xml_all_for_identity(xml_text: str | None) -> dict[str, str]:
        """Parse XML 'All' to get iLO MAC (for unique_id) and ProLiant model."""
        out = {"ilo_mac": "", "server_model": ""}
        if not xml_text:
            return out
        try:
            root = ET.fromstring(xml_text)
            # Model
            server_model = root.findtext(".//SPN") or ""
            out["server_model"] = server_model.strip()
            # Find NIC where DESCRIPTION contains 'iLO'
            for nic in root.iter("NIC"):
                desc = (nic.findtext("DESCRIPTION") or "").lower()
                if "ilo" in desc:
                    mac = nic.findtext("MACADDR") or ""
                    out["ilo_mac"] = mac.strip().lower()
                    break
        except Exception:
            pass
        return out

    
    async def _probe_endpoints(self, hass: HomeAssistant, host: str) -> dict[str, Any]:
        _LOGGER.debug("Probing endpoints for host: %s", host)
        session = async_get_clientsession(hass, verify_ssl=False)
        info = {"basic": {}, "redfish_name": "", "redfish_fw": "", "xml": {}, "redfish_available": False}

        # BasicDevice
        basic_url = f"http://{host}{BASICDEVICE_PATH}"
        basic_xml = await self._fetch_text(session, basic_url, verify_ssl=False)
        if basic_xml:
            _LOGGER.debug("Fetched BasicDevice.xml from %s", basic_url)
        else:
            _LOGGER.debug("BasicDevice.xml not found at %s", basic_url)
        info["basic"] = self._parse_basicdevice(basic_xml)

        # Redfish root
        redfish_url = f"https://{host}{REDFISH_ROOT}"
        try:
            async with async_timeout.timeout(8.0):
                async with session.get(redfish_url, ssl=False) as resp:
                    _LOGGER.debug("Redfish probe status %s for %s", resp.status, redfish_url)
                    if resp.status < 300:
                        info["redfish_available"] = True
        except Exception as e:
            _LOGGER.warning("Redfish probe failed for %s: %s", host, e)

        # XML All
        xml_url = f"https://{host}{XML_ALL_PATH}"
        xml_text = await self._fetch_text(session, xml_url, verify_ssl=False)
        if xml_text:
            _LOGGER.debug("Fetched XML All from %s", xml_url)
        else:
            _LOGGER.debug("XML All not found at %s", xml_url)
        info["xml"] = self._parse_xml_all_for_identity(xml_text)

        return info


    # NEW: robust Redfish session auth + probe
    async def _redfish_auth_probe(
        self,
        hass: HomeAssistant,
        host: str,
        username: str,
        password: str,
    ) -> dict[str, Any]:
        """
        Try Redfish session login and probe Systems/Managers.
        Returns dict like {"success": bool, "error": str|None, "server_name": str|None, "product_name": str|None}.
        """
        session = async_get_clientsession(hass, verify_ssl=False)
        base = f"https://{host}"
        token: str | None = None
        session_uri: str | None = None

        async def _login(path: str) -> tuple[Optional[str], Optional[str], Optional[int]]:
            try:
                async with async_timeout.timeout(10.0):
                    resp = await session.post(
                        base + path,
                        json={"UserName": username, "Password": password},
                        headers={"Content-Type": "application/json", "Accept": "application/json"},
                        ssl=False,
                    )
                    if resp.status in (200, 201):
                        return resp.headers.get("X-Auth-Token"), resp.headers.get("Location"), resp.status
                    return None, None, resp.status
            except Exception:
                return None, None, None

        # Try canonical and alternate session endpoints
        for login_path in ("/redfish/v1/SessionService/Sessions", "/redfish/v1/Sessions"):
            token, session_uri, status = await _login(login_path)
            if token:
                break

        if not token:
            # Distinguish between cannot_connect and invalid_auth crudely (no status correlation here)
            return {"success": False, "error": "invalid_auth", "server_name": None, "product_name": None}

        async def _get(path: str) -> tuple[int, Any | None]:
            try:
                async with async_timeout.timeout(10.0):
                    resp = await session.get(
                        base + path,
                        headers={"X-Auth-Token": token, "Accept": "application/json"},
                        ssl=False,
                    )
                    if resp.status < 300:
                        try:
                            return resp.status, await resp.json(content_type=None)
                        except Exception:
                            return resp.status, None
                    return resp.status, None
            except Exception:
                return 0, None

        # Probe systems (try member and collection)
        product_name = None
        server_name = None

        status, data = await _get("/redfish/v1/Systems/1")
        if status >= 300 or data is None:
            status, data = await _get("/redfish/v1/Systems")

            # Follow first member if present
            if status < 300 and data and isinstance(data.get("Members"), list) and data["Members"]:
                member = data["Members"][0].get("@odata.id")
                if member:
                    _, data = await _get(member)

        if data:
            product_name = data.get("Model") or data.get("Name") or None
            server_name = data.get("HostName") or data.get("Name") or None

        # Probe managers (optional nice-to-have)
        status_mgr, mgr = await _get("/redfish/v1/Managers/1")
        if status_mgr >= 300 or mgr is None:
            status_mgr, mgr = await _get("/redfish/v1/Managers")
            if status_mgr < 300 and mgr and isinstance(mgr.get("Members"), list) and mgr["Members"]:
                member = mgr["Members"][0].get("@odata.id")
                if member:
                    _, mgr = await _get(member)

        # Clean up session (best effort)
        try:
            if session_uri:
                await session.delete(base + session_uri, headers={"X-Auth-Token": token}, ssl=False)
        except Exception:
            pass

        return {"success": True, "error": None, "server_name": server_name, "product_name": product_name}

    # NEW: normalize redfish test for user/confirm steps
    async def _test_redfish_connection(
        self, hass: HomeAssistant, host: str, user: str, password: str
    ) -> dict[str, Any]:
        try:
            return await self._redfish_auth_probe(hass, host, user, password)
        except Exception:
            return {"success": False, "error": "cannot_connect", "server_name": None, "product_name": None}

    # --------------------------
    # User init step
    # --------------------------
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            user_input[CONF_PORT] = port

            # Try to derive stable unique_id from iLO MAC (unauth probe); fallback to host:port
            uniq = None
            try:
                info = await self._probe_endpoints(self.hass, host)
                ilo_mac = info.get("xml", {}).get("ilo_mac") or ""
                if ilo_mac:
                    uniq = f"ilo-{ilo_mac.replace(':','')}"
            except Exception:
                pass
            unique_id = uniq or f"{host}:{port}"

            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured(
                updates={
                    CONF_HOST: host,
                    CONF_USERNAME: user_input[CONF_USERNAME],
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_PORT: port,
                }
            )

            # First: try legacy hpilo channel (RIBCL/XML)
            result = await self.hass.async_add_executor_job(
                self._test_connection,
                host,
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                port,
            )

            api_used = "hpilo"
            # Fallback: if hpilo fails, try Redfish
            _LOGGER.info("hpilo failed, trying Redfish fallback for host %s", host)
            if not result["success"]:
                rf = await self._test_redfish_connection(
                    self.hass, host, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
                if rf["success"]:
                    result = rf
                    api_used = "redfish"

            if result["success"]:
                product_name = result.get("product_name")
                server_name = result.get("server_name")

                if product_name:
                    if server_name and server_name.lower() not in [host.lower(), "localhost", "unknown"]:
                        title = f"{product_name} ({server_name})"
                    else:
                        title = product_name
                elif server_name and server_name.lower() not in [host.lower(), "localhost", "unknown"]:
                    title = server_name
                else:
                    title = f"iLO {host}"

                # NEW: persist which API worked so the integration can choose the appropriate client
                data = dict(user_input)
                data["api"] = api_used  # "hpilo" or "redfish"

                return self.async_create_entry(title=title, data=data)

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

    # --------------------------
    # SSDP discovery step
    # --------------------------
    async def async_step_ssdp(
        self, discovery_info: ssdp.SsdpServiceInfo
    ) -> FlowResult:
        _LOGGER.info("SSDP discovery triggered: %s", discovery_info)
        """Handle SSDP discovery."""
        location = getattr(discovery_info, "ssdp_location", None) or getattr(discovery_info, "location", None)
        host = self._parse_host_from_location(location)
        if not host:
            return self.async_abort(reason="no_host")

        # Extract friendly name and model (may be empty)
        name = discovery_info.upnp.get(ssdp.ATTR_UPNP_FRIENDLY_NAME, "") or ""
        model = discovery_info.upnp.get(ssdp.ATTR_UPNP_MODEL_NAME, "") or ""

        # Probe endpoints to enrich and derive a stable unique_id
        info = await self._probe_endpoints(self.hass, host)
        friendly = info["basic"].get("friendlyName") or name or host
        model_name = info["xml"].get("server_model") or info["basic"].get("modelName") or model or ""

        ilo_mac = info["xml"].get("ilo_mac") or ""
        if ilo_mac:
            unique_id = f"ilo-{ilo_mac.replace(':','')}"
        else:
            unique_id = f"{host}:{DEFAULT_PORT}"

        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})

        # Persist for confirm step
        self._discovered_host = host
        self._discovered_name = friendly
        self._discovered_description = model_name
        self._discovered_mac = ilo_mac or None

        # Title placeholders
        self.context["title_placeholders"] = {
            "name": self._discovered_name,
            "host": host,
            "description": self._discovered_description or "HP iLO",
        }

        return await self.async_step_confirm()

    # --------------------------
    # Confirmation step
    # --------------------------
    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle user confirmation of discovered device."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Try hpilo first, then fall back to Redfish if hpilo cannot connect/auth
            result = await self.hass.async_add_executor_job(
                self._test_connection,
                self._discovered_host,
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
                DEFAULT_PORT,
            )

            api_used = "hpilo"
            if not result["success"]:
                rf = await self._test_redfish_connection(
                    self.hass, self._discovered_host, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
                if rf["success"]:
                    result = rf
                    api_used = "redfish"

            if result["success"]:
                server_name = result.get("server_name")

                if self._discovered_name and self._discovered_name != self._discovered_host:
                    title = self._discovered_name
                elif server_name and server_name.lower() not in [
                    self._discovered_host.lower(),
                    "localhost",
                    "unknown",
                ]:
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
                        "api": api_used,  # NEW
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
                "name": self._discovered_name or "HP iLO",
                "host": self._discovered_host or "",
                "description": self._discovered_description or "HP iLO",
            },
            errors=errors,
        )

    # --------------------------
    # Connection test (unchanged for hpilo)
    # --------------------------
    def _test_connection(
        self, host: str, user: str, password: str, port: int
    ) -> dict[str, Any]:
        """Test connection to HP iLO and return detailed error info (hpilo/RIBCL path)."""
        try:
            if port == 80:
                port = 443

            ilo = hpilo.Ilo(host, user, password, port=port)

            server_name = ilo.get_server_name()
            product_name = None
            try:
                host_data = ilo.get_host_data()
                for item in host_data:
                    if isinstance(item, dict) and item.get("Product Name"):
                        product_name = item["Product Name"]
                        break
            except Exception:
                pass  # optional

            return {"success": True, "error": None, "server_name": server_name, "product_name": product_name}

        except hpilo.IloLoginFailed:
            return {"success": False, "error": "invalid_auth", "server_name": None, "product_name": None}
        except hpilo.IloCommunicationError:
            return {"success": False, "error": "cannot_connect", "server_name": None, "product_name": None}
        except hpilo.IloError:
            return {"success": False, "error": "unknown", "server_name": None, "product_name": None}
        except Exception:
            return {"success": False, "error": "unknown", "server_name": None, "product_name": None}
