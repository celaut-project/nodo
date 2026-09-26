from typing import Optional, Dict
import os, json, requests

from src.utils.config import ConfigManager
from src.utils.logger import LOGGER as logger
from concurrent.futures import ThreadPoolExecutor

from src.utils.network import internet_available

env_manager = ConfigManager()

#: Ergo mainnet's conventional P2P port. Not a setting: a node's own P2P address is
#: never reported by anything this node can ask directly (see ``_p2p_address``
#: below, fed from the ``/peers/connected`` crawl -- the only place it is genuinely
#: *observed*), so this is the last-resort guess ``pow_networks._p2p_uri_for`` makes
#: for a candidate nobody's crawl ever saw on the wire: ``ledgers.ergo.NODE_URL``,
#: ``service_networks.default_instances``, or a peer's own ``ResolveNetwork``
#: answer. 9030 is what the reference node ships with, and observation bears that
#: out without making it a rule -- of 58 peers on one live mainnet node, 53 were on
#: 9030 and five were not (9020, 9029, 9031, 1540). Those five are exactly why an
#: observed port always wins over this one, and why guessing wrong for them is an
#: accepted cost rather than something an operator is asked to tune away: nothing
#: here can tell which five nodes exist to bias the guess towards.
MAINNET_P2P_PORT = 9030


def __available_ergo_node(url: Optional[str]) -> Optional[Dict]:
    ergo_node_url = env_manager.get("ledgers.ergo.NODE_URL") if not url else url
    try:
        info_url = f"{ergo_node_url}/info"
        response = requests.get(info_url)
        response.raise_for_status() 

        data = response.json()

        if data.get("genesisBlockId") == env_manager.get("ledgers.ergo.GENESIS_BLOCK_ID"):
            return {
                "isMining": data.get("isMining", False),
                "parameters": data.get("parameters", {}),
                "eip27Supported": data.get("eip27Supported", False),
                "appVersion": data.get("appVersion", "unknown")
            }
        else:
            logger(f"Ergo node {ergo_node_url} is not on the mainnet or has an incorrect genesis block ID. The genesisBlockId is {data.get('genesisBlockId')}, expected {env_manager.get('ledgers.ergo.GENESIS_BLOCK_ID')}.")
            return None
    except requests.exceptions.RequestException as e:
        logger(f"Error connecting to Ergo node: {e}")
        return None

def _p2p_address(peer: Dict) -> Optional[str]:
    """``host:port`` of a ``/peers/connected`` entry's P2P endpoint, or None.

    Ergo serializes the field as Java's ``InetSocketAddress.toString()``, so what
    arrives is ``/1.2.3.4:9030`` when the peer was reached by address and
    ``name/1.2.3.4:9030`` when a name was resolved first. The part after the last
    ``/`` is the one that was actually connected to, which is the one worth keeping.

    This is the only place the P2P port is *observed* rather than assumed. A node's
    own ``/info`` does not carry its P2P address -- it reports ``restApiUrl`` and
    nothing else addressable -- so a peer learned from anywhere but this crawl has no
    observed port and gets the configured default instead (see
    ``src/manager/pow_networks.py``).
    """
    raw = peer.get("address")
    if not isinstance(raw, str):
        return None
    candidate = raw.rsplit("/", 1)[-1].strip()
    # Rightmost colon: an IPv6 literal is bracketed, so this is the port separator.
    host, separator, port = candidate.rpartition(":")
    if not separator or not host:
        return None
    try:
        if not 0 < int(port) < 65536:
            return None
    except ValueError:
        return None
    return candidate


def get_refresh_peers() -> Dict[str, Dict]:
    http_peers_file = env_manager.get("ledgers.ergo.HTTP_PEERS_PATH")
    if not os.path.exists(http_peers_file):
        os.makedirs(os.path.dirname(http_peers_file), exist_ok=True)
        with open(http_peers_file, 'w') as f:
            f.write("{}")
    
    with open(http_peers_file, 'r') as f:
        peers = json.load(f)
        
    current_node = env_manager.get("ledgers.ergo.NODE_URL")
    if current_node not in peers:
        peers[current_node] = {}
    
    available_peers = {}
    checked_peers = set(peers.keys())
    
    def fetch_peers(url: str):
        try:
            response = requests.get(f"{url}/peers/connected", timeout=5)
            response.raise_for_status()
            data = response.json()
            
            for peer in data:
                rest_api_url = peer.get("restApiUrl")
                if rest_api_url and rest_api_url not in checked_peers:
                    checked_peers.add(rest_api_url)
                    
                    node_info = __available_ergo_node(rest_api_url)
                    if node_info:
                        # Added alongside the existing keys, never replacing the
                        # entry's shape: a file written by an older nodo simply has
                        # no p2pAddress, and every reader already tolerates that.
                        p2p_address = _p2p_address(peer)
                        if p2p_address:
                            node_info["p2pAddress"] = p2p_address
                        available_peers[rest_api_url] = node_info
                        logger(f"Found available Ergo node: {rest_api_url}")
                        fetch_peers(rest_api_url)
        except requests.RequestException as e:
            logger(f"Error fetching peers from {url}: {e}")
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        executor.map(fetch_peers, peers.keys())
    
    with open(http_peers_file, 'w') as f:
        json.dump(available_peers, f)
        
    return available_peers
    
def check_ergo_node_availability():
    """
    Checks the availability of the current Ergo node. If the current node is not available,
    it attempts to find a new available node from refreshed peers and updates the environment
    variable "ledgers.ergo.NODE_URL" with the new node URL.
    - Retrieves the current Ergo node URL from the environment.
    - Checks if the current Ergo node is available.
    - If not available, logs the unavailability and fetches a list of refreshed available peers.
    - If no available peers are found and the current node URL has not been manually changed,
      logs the absence of available nodes and clears the "ledgers.ergo.NODE_URL" environment variable.
    - If available peers are found, updates the "ledgers.ergo.NODE_URL" environment variable with the
      first available peer and logs the update.
    Note: Check for equality in case it has been manually changed.
    """
    
    if not internet_available():
        return
    
    current_ergo_node = env_manager.get("ledgers.ergo.NODE_URL")
    if __available_ergo_node(current_ergo_node):
        return
    
    logger(f"Ergo node {current_ergo_node} is not available.")
    availables = get_refresh_peers()  # New refreshed available peers.
    
    if not availables and current_ergo_node == env_manager.get("ledgers.ergo.NODE_URL"): 
        logger("No available Ergo nodes found.")
        env_manager.set("ledgers.ergo.NODE_URL", "")
        return
    
    new_ergo_node_url = next(iter(availables))
    env_manager.set("ledgers.ergo.NODE_URL", new_ergo_node_url)
    logger(f"ledgers.ergo.NODE_URL has been updated to {new_ergo_node_url}")
