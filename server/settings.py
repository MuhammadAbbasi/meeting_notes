"""Hub settings: env vars give the defaults, data/settings.json (edited from the dashboard) overrides them.

Everything here is hub-side, so a change applies to every workstation at once, without restarts
(except the Whisper model, which the hub applies by restarting the WhisperLive container).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# key -> (env var, default, type). type: str | int | float | dict | "secret" | "choice:a,b"
FIELDS: dict[str, tuple[str, Any, Any]] = {
    "transcriber": ("TRANSCRIBER", "whisperlive", "choice:whisperlive,mock"),
    "whisperlive_url": ("WHISPERLIVE_URL", "ws://whisperlive:9090", str),
    "whisper_model": ("WHISPER_MODEL", "deepdml/faster-whisper-large-v3-turbo-ct2", str),
    "ollama_url": ("OLLAMA_URL", "http://host.docker.internal:11434", str),
    "ollama_model": ("OLLAMA_MODEL", "qwen3.5:9b", str),
    "odoo_url": ("ODOO_URL", "", str),
    "odoo_db": ("ODOO_DB", "", str),
    "odoo_user": ("ODOO_USER", "", str),
    "odoo_api_key": ("ODOO_API_KEY", "", "secret"),
    "odoo_project_id": ("ODOO_PROJECT_ID", None, int),
    "odoo_knowledge_parent": ("ODOO_KNOWLEDGE_PARENT", "Meeting Notes", str),
    "speaker_aliases": ("SPEAKER_ALIASES", {}, dict),
    "dedup_hold_s": ("DEDUP_HOLD_S", 2.5, float),
    "dedup_window_s": ("DEDUP_WINDOW_S", 2.0, float),
    "dedup_similarity": ("DEDUP_SIMILARITY", 0.78, float),
    "noise_floor_rms": ("NOISE_FLOOR_RMS", 0.005, float),
    "partial_commit_s": ("PARTIAL_COMMIT_S", 3.0, float),
    "same_room_ratio": ("SAME_ROOM_RATIO", 5.0, float),
    "default_meeting": ("DEFAULT_MEETING", "daily", str),
    "default_language": ("DEFAULT_LANGUAGE", "it", str),
}
SECRETS = {k for k, (_, _, t) in FIELDS.items() if t == "secret"}


def _coerce(key: str, value: Any) -> Any:
    typ = FIELDS[key][2]
    if value is None or value == "":
        return None if typ is int else ("" if typ in (str, "secret") or isinstance(typ, str) else FIELDS[key][1])
    if typ is dict:
        value = json.loads(value) if isinstance(value, str) else value
        if not isinstance(value, dict):
            raise ValueError(f"{key} must be a JSON object")
        return {str(k).strip(): str(v).strip() for k, v in value.items()}
    if typ is int:
        return int(value)
    if typ is float:
        return float(value)
    if isinstance(typ, str) and typ.startswith("choice:"):
        choices = typ[7:].split(",")
        if value not in choices:
            raise ValueError(f"{key} must be one of {choices}")
        return value
    return str(value).strip()


class Settings:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.version = 0  # bumped on every saved change; lets two admins editing at once detect each other
        self.values: dict[str, Any] = {}
        for key, (env, default, _) in FIELDS.items():
            raw = os.getenv(env)
            self.values[key] = _coerce(key, raw) if raw not in (None, "") else default
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.values.update({k: _coerce(k, v) for k, v in saved.items() if k in FIELDS})

    def get(self, key: str) -> Any:
        return self.values[key]

    def public(self) -> dict[str, Any]:
        """Values safe to show: secrets replaced by whether they are set."""
        return {k: (bool(v) if k in SECRETS else v) for k, v in self.values.items()}

    def update(self, patch: dict[str, Any]) -> list[str]:
        """Apply and persist known keys; a secret sent as "" keeps its value, None clears it. Returns changed keys."""
        # validate everything first: one bad field must not leave the others half-applied
        coerced = {k: _coerce(k, v) for k, v in patch.items() if k in FIELDS and not (k in SECRETS and v == "")}
        changed = [k for k, v in coerced.items() if v != self.values[k]]
        if changed:
            self.values.update({k: coerced[k] for k in changed})
            self.version += 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.values, indent=2), encoding="utf-8")
            tmp.replace(self.path)  # atomic: a crash never leaves a torn settings file
        return changed
