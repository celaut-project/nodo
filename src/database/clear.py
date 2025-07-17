import os

from src.utils.config import ConfigManager

env_manager = ConfigManager()

DATABASE_FILE = env_manager.get("DATABASE_FILE")

os.remove(DATABASE_FILE)

print("Database dropped.")
