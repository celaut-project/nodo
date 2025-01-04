from typing import Optional, List, Dict
import os, json, socket, urllib

from src.utils.env import EnvManager
from src.utils.logger import LOGGER as log

env_manager = EnvManager()


def __available_ergo_node(url: Optional[str]) -> Optional[Dict]:
    ergo_node_url = env_manager.get_env("ERGO_NODE_URL") if not url else url
    try:
        response = socket.create_connection((ergo_node_url.split(":")[0], int(ergo_node_url.split(":")[1])), timeout=5)
        response.close()
        
        # Check the /info endpoint
        info_url = f"{ergo_node_url}/info"
        with urllib.request.urlopen(info_url) as response:
            data = json.loads(response.read().decode())
            if data.get("network") == "mainnet" and data.get("genesisBlockId") == env_manager.get_env("ERGO_GENESIS_BLOCK_ID"):
                return {
                    "isMining": data.get("isMining", False),
                    "parameters": data.get("parameters", {}),
                    "eip27Supported": data.get("eip27Supported", False),
                    "appVersion": data.get("appVersion", "unknown")
                }
            else:
                log(f"Ergo node {ergo_node_url} is not on the mainnet or has an incorrect genesis block ID.")
                return None
    except Exception as e:
        log(f"Error connecting to Ergo node: {e}")
        return None

def __get_refresh_peers() -> Dict[str, Dict]:
    http_peers_file = env_manager.get_env("ERGO_HTTP_PEERS")
    if not os.path.exists(http_peers_file):
        with open(http_peers_file, 'w') as f:
            f.write("{}")
    
    with open(http_peers_file, 'r') as f:
        peers = json.load(f)
    
    available_peers = {}
    for peer, info in peers.items():
        node_info = __available_ergo_node(peer)
        if node_info:
            available_peers[peer] = node_info
    
    with open(http_peers_file, 'w') as f:
        json.dump(available_peers, f)
        
    return available_peers
    
def check_ergo_node_availability():
    if __available_ergo_node(None):
        return
    
    log(f"Ergo node {env_manager.get_env('ERGO_NODE_URL')} is not available.")
    availables = __get_refresh_peers()  # New refreshed available peers.
    
    if not availables:
        log("No available Ergo nodes found.")
        env_manager.write_env("ERGO_NODE_URL", "")
        return
    
    new_ergo_node_url = next(iter(availables))
    env_manager.write_env("ERGO_NODE_URL", new_ergo_node_url)
    log(f"ERGO_NODE_URL has been updated to {new_ergo_node_url}")
