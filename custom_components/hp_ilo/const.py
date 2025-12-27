"""Constants for the HP iLO integration."""

# Integration domain
DOMAIN = "hp_ilo"

# Default configuration values
DEFAULT_PORT = 443  # HTTPS/SSL (RIBCL protocol requires encryption)
DEFAULT_SCAN_INTERVAL = 60  # Polling interval in seconds (1 minute default)

# Port 80 (HTTP) is not supported by iLO RIBCL protocol and will be
# automatically upgraded to 443 (HTTPS) in the coordinator

# Common attribute keys
ATTR_MODEL = "model"
ATTR_SERIAL = "serial_number"

# Configuration options
CONF_SCAN_INTERVAL = "scan_interval"  # User-configurable polling interval

# Scan interval constraints
MIN_SCAN_INTERVAL = 30  # Minimum 30 seconds to avoid overloading iLO
MAX_SCAN_INTERVAL = 3600  # Maximum 1 hour