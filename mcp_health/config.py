import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "changeme")
DB_PATH = str(BASE_DIR / os.environ.get("DB_PATH", "data/fitness.db"))
TZ = os.environ.get("TZ", "America/Toronto")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
OFF_COUNTRY = os.environ.get("OFF_COUNTRY", "")  # e.g. "en:canada" (used by import script)
OAUTH_ISSUER = os.environ.get("OAUTH_ISSUER", "")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
