import os

from src.utils.env import DATABASE_FILE

os.remove(DATABASE_FILE)

print("Database dropped.")
