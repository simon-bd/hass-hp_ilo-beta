"""Config flow for HP iLO."""
import voluptuous as vol
import hpilo
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_USERNAME, CONF_PASSWORD, CONF_PORT, CONF_NAME
from .const import DOMAIN, DEFAULT_PORT

class HpIloFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            # Force port to int before testing
            user_input[CONF_PORT] = int(user_input.get(CONF_PORT, DEFAULT_PORT))
            
            valid = await self.hass.async_add_executor_job(
                self._test, user_input[CONF_HOST], user_input[CONF_USERNAME], 
                user_input[CONF_PASSWORD], user_input[CONF_PORT]
            )
            if valid:
                return self.async_create_entry(title=user_input[CONF_HOST], data=user_input)
            errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_USERNAME, default="Administrator"): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
            }),
            errors=errors,
        )

    def _test(self, host, user, password, port):
        try:
            ilo = hpilo.Ilo(host, user, password, port=port)
            ilo.get_server_name()
            return True
        except Exception:
            return False