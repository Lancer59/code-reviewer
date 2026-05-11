"""
Central configuration loader for Dev Companion.

Priority (highest → lowest):
  1. config.json  — present next to this file (or at CONFIG_FILE env var path)
  2. Environment variables / .env file
  3. Built-in defaults

Usage anywhere in the codebase:
    from config import cfg
    api_key = cfg("AZURE_OPENAI_API_KEY")
    base_url = cfg("APP_BASE_URL", "http://localhost:8001")

When mounting inside another FastAPI app, the host can write a config.json
(or point CONFIG_FILE at one) before importing any Dev Companion module:
    import os
    os.environ["CONFIG_FILE"] = "/path/to/my/reviewer-config.json"
    from dashboard.api import dashboard_app
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("config")

_HERE = Path(__file__).parent
_CONFIG_FILE = Path(os.getenv("CONFIG_FILE", str(_HERE / "config.json")))

_json_config: dict[str, Any] = {}

if _CONFIG_FILE.exists():
    try:
        with open(_CONFIG_FILE, "r", encoding="utf-8") as _f:
            _json_config = json.load(_f)
        logger.info("Loaded config from %s", _CONFIG_FILE)
    except Exception as _e:
        logger.warning("Failed to load %s: %s — falling back to env vars", _CONFIG_FILE, _e)
else:
    logger.debug("No config.json found at %s — using env vars / defaults", _CONFIG_FILE)


def cfg(key: str, default: Any = None) -> Any:
    """
    Get a configuration value.
    Lookup order: config.json → os.environ → default
    """
    if key in _json_config:
        return _json_config[key]
    return os.environ.get(key, default)


def cfg_bool(key: str, default: bool = False) -> bool:
    """Get a boolean config value. Accepts true/false/1/0/yes/no (case-insensitive)."""
    val = cfg(key)
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    return str(val).lower() in ("true", "1", "yes")


def cfg_int(key: str, default: int = 0) -> int:
    """Get an integer config value."""
    val = cfg(key)
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        logger.warning("Config key %s has non-integer value %r — using default %d", key, val, default)
        return default


def reload(config_file: str | Path = None) -> None:
    """Reload configuration from a (new) config.json path."""
    global _json_config, _CONFIG_FILE
    if config_file:
        _CONFIG_FILE = Path(config_file)
    _json_config = {}
    if _CONFIG_FILE.exists():
        try:
            with open(_CONFIG_FILE, "r", encoding="utf-8") as f:
                _json_config = json.load(f)
            logger.info("Reloaded config from %s", _CONFIG_FILE)
        except Exception as e:
            logger.warning("Failed to reload %s: %s", _CONFIG_FILE, e)
