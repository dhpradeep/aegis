import hashlib
import secrets


def generate_api_key() -> tuple[str, str, str]:
    full = "cak_" + secrets.token_urlsafe(32)
    return full, full[:12], hash_key(full)


def hash_key(full: str) -> str:
    return hashlib.sha256(full.encode()).hexdigest()
