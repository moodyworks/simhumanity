"""Configuration, loaded from environment / .env.

Everything that differs between this WSL2 workstation and the future Debian VPS
lives here, sourced from env vars — nothing is hardcoded to a user or path.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so we avoid an extra dependency."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


# Load .env sitting next to the project root (one level up from this file).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_load_dotenv(_PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    host: str
    port: int
    tick_hz: float
    minutes_per_tick: int
    ticks_per_era: int
    ai_provider: str
    deepseek_api_key: str
    deepseek_model: str
    deepseek_base_url: str
    ollama_model: str
    ollama_base_url: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "simhumanity.sqlite3"


def load_settings() -> Settings:
    default_data = Path.home() / ".local" / "share" / "simhumanity"
    data_dir = Path(os.environ.get("SIMHUMANITY_DATA_DIR", str(default_data)))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=data_dir,
        host=os.environ.get("SIMHUMANITY_HOST", "127.0.0.1"),
        port=int(os.environ.get("SIMHUMANITY_PORT", "8000")),
        tick_hz=float(os.environ.get("SIMHUMANITY_TICK_HZ", "3")),
        minutes_per_tick=int(os.environ.get("SIMHUMANITY_MINUTES_PER_TICK", "30")),
        ticks_per_era=int(os.environ.get("SIMHUMANITY_TICKS_PER_ERA", "1350")),
        ai_provider=os.environ.get("SIMHUMANITY_AI_PROVIDER", "auto"),
        deepseek_api_key=os.environ.get("SIMHUMANITY_DEEPSEEK_API_KEY", ""),
        deepseek_model=os.environ.get("SIMHUMANITY_DEEPSEEK_MODEL", "deepseek-chat"),
        deepseek_base_url=os.environ.get(
            "SIMHUMANITY_DEEPSEEK_BASE_URL", "https://api.deepseek.com"
        ),
        ollama_model=os.environ.get("SIMHUMANITY_OLLAMA_MODEL", "llama3.1:8b"),
        ollama_base_url=os.environ.get(
            "SIMHUMANITY_OLLAMA_BASE_URL", "http://127.0.0.1:11434"
        ),
    )


SETTINGS = load_settings()
