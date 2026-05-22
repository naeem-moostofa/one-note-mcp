from cryptography.fernet import Fernet

from app.core.config import settings


def encrypt(value: str) -> str:
    return Fernet(settings.TOKEN_ENCRYPTION_KEY.encode()).encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return Fernet(settings.TOKEN_ENCRYPTION_KEY.encode()).decrypt(value.encode()).decode()
