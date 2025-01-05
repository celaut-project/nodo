from typing import Optional, List, Dict
import os, json, requests

from src.utils.env import EnvManager
from src.utils.logger import LOGGER as log

env_manager = EnvManager()


def __available_ergo_node(url: Optional[str]) -> Optional[Dict]:
    ergo_node_url = env_manager.get_env("ERGO_NODE_URL") if not url else url
    try:
        info_url = f"{ergo_node_url}/info"
        response = requests.get(info_url)
        response.raise_for_status() 

        data = response.json()

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
    except requests.exceptions.RequestException as e:
        log(f"Error connecting to Ergo node: {e}")
        return None

def get_refresh_peers() -> Dict[str, Dict]:
    http_peers_file = env_manager.get_env("ERGO_HTTP_PEERS")
    if not os.path.exists(http_peers_file):
        with open(http_peers_file, 'w') as f:
            f.write("{}")
    
    with open(http_peers_file, 'r') as f:
        peers = json.load(f)
        
    current_node = env_manager.get_env("ERGO_NODE_URL")
    if current_node not in peers:
        peers[current_node] = {}
    
    available_peers = {}
    checked_peers = set(peers.keys())
    
    def fetch_peers(url: str):
        try:
            response = requests.get(f"{url}/peers/connected", timeout=10)
            response.raise_for_status()
            data = response.json()
            
            for peer in data:
                rest_api_url = peer.get("restApiUrl")
                if rest_api_url and rest_api_url not in checked_peers:
                    checked_peers.add(rest_api_url)
                    
                    node_info = __available_ergo_node(rest_api_url)
                    if node_info:
                        available_peers[rest_api_url] = node_info
                        log(f"Found available Ergo node: {rest_api_url}")
                        fetch_peers(rest_api_url)
        except requests.RequestException as e:
            log(f"Error fetching peers from {url}: {e}")
    
    for peer in peers.keys():
        fetch_peers(peer)
    
    with open(http_peers_file, 'w') as f:
        json.dump(available_peers, f)
        
    return available_peers
    
def check_ergo_node_availability():
    """
    Checks the availability of the current Ergo node. If the current node is not available,
    it attempts to find a new available node from refreshed peers and updates the environment
    variable "ERGO_NODE_URL" with the new node URL.
    - Retrieves the current Ergo node URL from the environment.
    - Checks if the current Ergo node is available.
    - If not available, logs the unavailability and fetches a list of refreshed available peers.
    - If no available peers are found and the current node URL has not been manually changed,
      logs the absence of available nodes and clears the "ERGO_NODE_URL" environment variable.
    - If available peers are found, updates the "ERGO_NODE_URL" environment variable with the
      first available peer and logs the update.
    Note: Check for equality in case it has been manually changed.
    """
    
    log("Checking Ergo node availability...")
    
    current_ergo_node = env_manager.get_env("ERGO_NODE_URL")
    if __available_ergo_node(current_ergo_node):
        return
    
    log(f"Ergo node {current_ergo_node} is not available.")
    availables = get_refresh_peers()  # New refreshed available peers.
    
    if not availables and current_ergo_node == env_manager.get_env("ERGO_NODE_URL"): 
        log("No available Ergo nodes found.")
        env_manager.write_env("ERGO_NODE_URL", "")
        return
    
    new_ergo_node_url = next(iter(availables))
    env_manager.write_env("ERGO_NODE_URL", new_ergo_node_url)
    log(f"ERGO_NODE_URL has been updated to {new_ergo_node_url}")
