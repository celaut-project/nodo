from src.utils.env import EnvManager


env_manager = EnvManager()
LEDGER = "ergo" # or "ergo-testnet" for Ergo testnet.
CONTRACT = open("contracts/reputation_system.es", "r").read()
