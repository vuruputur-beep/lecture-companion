"""Application configuration and secret handling.

Non-secret settings live in data/config.json (editable from the Settings page).
Secrets live in data/secrets.env with 0600 permissions, written by the local
Settings page - never committed, never sent to the browser.
Environment variables override both.
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("LC_DATA_DIR", PROJECT_ROOT / "data")).resolve()
LECTURES_DIR = DATA_DIR / "lectures"
CONFIG_PATH = DATA_DIR / "config.json"
SECRETS_PATH = DATA_DIR / "secrets.env"
DB_PATH = DATA_DIR / "app.db"

DEFAULT_CONFIG: dict[str, Any] = {
    # AI provider: "anthropic" | "openai" | "openai_compatible"
    "provider": "anthropic",
    "model": "claude-opus-5-5",
    "effort": "medium",               # low|medium|high|xhigh|max (Anthropic only)
    "openai_base_url": "",           # for openai_compatible (e.g. http://localhost:11434/v1)
    "max_concurrent_jobs": 2,
    "concept_parallelism": 3,        # concept entries generated concurrently within one lecture job
    "chunk_words": 7000,             # transcript words per analysis chunk
    "spend_limit_usd_per_lecture": 15.0,
    "spend_limit_usd_total": 200.0,
    "study_session_minutes": 20,
    "timezone": "America/Los_Angeles",
    "default_course": "Interview Kickstart - Forward Deployed Engineer",
    "verify_links": True,
    "host": "127.0.0.1",
    "port": 8765,
}

SECRET_KEYS = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_COMPATIBLE_API_KEY"]


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LECTURES_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    ensure_dirs()
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text()))
        except json.JSONDecodeError:
            pass
    # env overrides (LC_<KEY>)
    for key in DEFAULT_CONFIG:
        env_val = os.environ.get(f"LC_{key.upper()}")
        if env_val is not None:
            default = DEFAULT_CONFIG[key]
            if isinstance(default, bool):
                cfg[key] = env_val.lower() in ("1", "true", "yes")
            elif isinstance(default, int):
                cfg[key] = int(env_val)
            elif isinstance(default, float):
                cfg[key] = float(env_val)
            else:
                cfg[key] = env_val
    return cfg


def save_config(updates: dict[str, Any]) -> dict[str, Any]:
    ensure_dirs()
    cfg = load_config()
    for k, v in updates.items():
        if k in DEFAULT_CONFIG:
            default = DEFAULT_CONFIG[k]
            try:
                if isinstance(default, bool):
                    cfg[k] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
                elif isinstance(default, int):
                    cfg[k] = int(v)
                elif isinstance(default, float):
                    cfg[k] = float(v)
                else:
                    cfg[k] = str(v)
            except (TypeError, ValueError):
                continue
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    return cfg


def _read_secrets_file() -> dict[str, str]:
    out: dict[str, str] = {}
    if SECRETS_PATH.exists():
        for line in SECRETS_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def get_secret(name: str) -> str | None:
    """Env var wins; otherwise the local secrets file."""
    val = os.environ.get(name)
    if val:
        return val
    return _read_secrets_file().get(name) or None


def set_secret(name: str, value: str | None) -> None:
    ensure_dirs()
    secrets = _read_secrets_file()
    if value:
        secrets[name] = value.strip()
    else:
        secrets.pop(name, None)
    body = "# Lecture Companion secrets - never commit this file\n" + "".join(
        f"{k}={v}\n" for k, v in secrets.items()
    )
    SECRETS_PATH.write_text(body)
    os.chmod(SECRETS_PATH, stat.S_IRUSR | stat.S_IWUSR)


def secret_status() -> dict[str, dict[str, Any]]:
    """Masked view for the Settings UI. Never returns the actual values."""
    status = {}
    file_secrets = _read_secrets_file()
    for k in SECRET_KEYS:
        env_set = bool(os.environ.get(k))
        file_set = bool(file_secrets.get(k))
        val = os.environ.get(k) or file_secrets.get(k) or ""
        status[k] = {
            "configured": bool(val),
            "source": "environment" if env_set else ("secrets file" if file_set else "not set"),
            "masked": (val[:7] + "…" + val[-4:]) if len(val) > 14 else ("set" if val else ""),
        }
    return status
