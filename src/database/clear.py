import os

from src.utils.env import EnvManager

env_manager = EnvManager()

DATABASE_FILE = env_manager.get_env("DATABASE_FILE")

os.remove(DATABASE_FILE)

print("Database dropped.")
