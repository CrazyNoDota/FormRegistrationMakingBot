import os
from dataclasses import dataclass

@dataclass
class Config:
    telegram_token: str
    nvidia_api_key: str
    database_path: str
    log_level: str
    allowed_user_id: int | None

_cfg: "Config | None" = None

def load() -> "Config":
    global _cfg
    if _cfg is None:
        allowed_user_id = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "").strip()
        _cfg = Config(
            telegram_token=os.environ["TELEGRAM_BOT_TOKEN"],
            nvidia_api_key=os.environ["NVIDIA_API_KEY"],
            database_path=os.environ.get("DATABASE_PATH", "/data/memory.db"),
            log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
            allowed_user_id=int(allowed_user_id) if allowed_user_id else None,
        )
    return _cfg

def get() -> "Config":
    return _cfg or load()
