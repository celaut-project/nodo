"""Service tunneling (``Gateway.ServiceTunnel``) — see ``docs/TUNNELING.md``.

Exposes the tunnel logger the modules in this package write through. Tunnel
logging is per-connection, per-datagram and per-billing-tick, which drowns the
node log on a busy tunnel and says nothing an operator asked for, so it is off
unless ``logs.TUNNEL_LOGS`` turns it on.
"""

from src.utils.config import ConfigManager
from src.utils.logger import LOGGER

logger = (
    LOGGER
    if ConfigManager().get("logs.TUNNEL_LOGS", False)
    else (lambda _message: None)
)
