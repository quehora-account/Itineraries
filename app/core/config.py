import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    API_KEY = os.getenv("API_KEY")
    AUTH_DOMAIN = os.getenv("AUTH_DOMAIN")
    PROJECT_ID = os.getenv("PROJECT_ID")
    STORAGE_BUCKET = os.getenv("STORAGE_BUCKET")
    MESSAGING_SENDER_ID = os.getenv("MESSAGING_SENDER_ID")
    APP_ID = os.getenv("APP_ID")
    MEASUREMENT_ID = os.getenv("MEASUREMENT_ID")
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    SPOTS_COLLECTION = 'spot'
    PLAYLISTS_COLLECTION = 'playlist'


settings = Settings()

assert settings.API_KEY is not None, "API_KEY is not set"
assert settings.AUTH_DOMAIN is not None, "AUTH_DOMAIN is not set"
assert settings.PROJECT_ID is not None, "PROJECT_ID is not set"
assert settings.STORAGE_BUCKET is not None, "STORAGE_BUCKET is not set"
assert settings.MESSAGING_SENDER_ID is not None, "MESSAGING_SENDER_ID is not set"
assert settings.APP_ID is not None, "APP_ID is not set"
assert settings.MEASUREMENT_ID is not None, "MEASUREMENT_ID is not set"
assert settings.OPENAI_API_KEY is not None, "OPENAI_API_KEY is not set"
