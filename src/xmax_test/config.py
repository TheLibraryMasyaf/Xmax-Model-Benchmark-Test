"""Read, merge and validate configuration files.

Config priority (highest first): CLI explicit arguments -> Run Request ->
project config -> ``.env`` secrets -> example defaults.

This module only loads configuration; it never reads business data. Unknown
keys are rejected so typos cannot be silently ignored. Paths are resolved
relative to the config file that declares them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaValidationError
from referencing import Registry, Resource

from .errors import ConfigError

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"

SECRET_KEYS = {
    "XMAX_API_KEY",
    "XMAX_API_SECRET",
    "api_key",
    "apiKey",
    "api_secret",
    "app_secret",
    "appSecret",
    "token",
    "password",
    "secret",
    "authorization",
    "access_token",
}


def load_json(path: str | Path) -> dict[str, Any]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} must contain a JSON object")
    return data


def load_config(
    path: str | Path,
    schema_name: str,
    *,
    base_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Load a JSON config, validate it against a schema in ``schemas/``.

    Paths inside the config are resolved relative to ``base_dir`` (defaults to
    the config file's directory). Validation errors are reported with the
    JSON pointer so operators can fix the offending key.
    """

    path = Path(path)
    data = load_json(path)
    schema_path = SCHEMA_DIR / schema_name
    if not schema_path.is_file():
        raise ConfigError(f"unknown schema {schema_name} for {path}")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema.setdefault("$id", schema_path.resolve().as_uri())
    registry = Registry()
    for candidate in SCHEMA_DIR.glob("*.json"):
        candidate_schema = json.loads(candidate.read_text(encoding="utf-8"))
        candidate_schema.setdefault("$id", candidate.resolve().as_uri())
        registry = registry.with_resource(
            candidate.resolve().as_uri(), Resource.from_contents(candidate_schema)
        )
    validator = Draft202012Validator(schema, registry=registry)
    errors = sorted(validator.iter_errors(data), key=lambda item: list(item.path))
    if errors:
        first = errors[0]
        pointer = "/".join(str(part) for part in first.path) or "<root>"
        raise ConfigError(
            f"config {path} fails {schema_name}: {pointer}: {first.message}"
        )

    base = Path(base_dir) if base_dir is not None else path.parent
    resolved = resolve_paths(data, base)
    resolved["_config_path"] = str(path.resolve())
    resolved["_config_base_dir"] = str(base.resolve())
    return resolved


def resolve_paths(value: Any, base_dir: Path) -> Any:
    """Recursively resolve known path-like keys to absolute paths."""

    path_keys = {
        "benchmark_path",
        "scenario_pack_path",
        "operation_recipe_path",
        "artifact_root",
        "manifest_root",
        "database_path",
        "manifest_directory",
        "report_template_path",
        "report_output_directory",
        "directory",
        "manifest_uri",
        "path",
        "existing_results_request_path",
        "report_template",
        "xmax_api_key_file",
    }
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key in path_keys and isinstance(item, str) and item:
                candidate = Path(item)
                if not candidate.is_absolute():
                    item = str((base_dir / candidate).resolve())
            elif key == "codex" and isinstance(item, dict) and "binary" in item:
                item = dict(item)
                binary = Path(item["binary"])
                if not binary.is_absolute():
                    item["binary"] = str((base_dir / binary).resolve())
            out[key] = resolve_paths(item, base_dir)
        return out
    if isinstance(value, list):
        return [resolve_paths(item, base_dir) for item in value]
    return value


def load_dotenv(path: str | Path) -> dict[str, str]:
    """Parse a ``.env`` file into a plain dict without loading any secrets."""

    result: dict[str, str] = {}
    dotenv = Path(path)
    if not dotenv.is_file():
        return result
    for line in dotenv.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


def env_secret(name: str, dotenv_path: str | Path | None = None) -> str | None:
    """Resolve a secret from the environment or an optional ``.env`` file."""

    value = os.environ.get(name)
    if value is not None:
        return value
    if dotenv_path is not None:
        return load_dotenv(dotenv_path).get(name)
    return None


def secret_file(path: str | Path | None) -> str | None:
    """Read one secret value from a file without logging its contents."""

    if not path:
        return None
    candidate = Path(path)
    if not candidate.is_file():
        return None
    value = candidate.read_text(encoding="utf-8").strip()
    return value or None


def redact(obj: Any) -> Any:
    """Recursively redact values under known secret keys for logs/JSON output."""

    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and key in SECRET_KEYS:
                out[key] = "***REDACTED***"
            else:
                out[key] = redact(value)
        return out
    if isinstance(obj, list):
        return [redact(item) for item in obj]
    return obj
