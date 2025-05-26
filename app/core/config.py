import os


class Settings:
    API_KEY = os.getenv("API_KEY")
    AUTH_DOMAIN = os.getenv("AUTH_DOMAIN")
    PROJECT_ID = os.getenv("PROJECT_ID")
    STORAGE_BUCKET = os.getenv("STORAGE_BUCKET")
    MESSAGING_SENDER_ID = os.getenv("MESSAGING_SENDER_ID")
    APP_ID = os.getenv("APP_ID")
    MEASUREMENT_ID = os.getenv("MEASUREMENT_ID")


settings = Settings()
