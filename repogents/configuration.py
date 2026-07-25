from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from . import mini_swe
from .mini_swe import model_api_key_variables

_SETTINGS_VERSION = 1
_MAX_ENDPOINT_LENGTH = 2_048
_MAX_MODEL_LENGTH = 512
_MAX_API_KEY_LENGTH = 16_384
_MAX_CATALOG_BYTES = 1_000_000
_MODEL_CATALOG_TIMEOUT = 10.0
_ROLE_FIELDS = {
    "lead": "lead_model",
    "implementer": "implementer_model",
    "verifier": "verifier_model",
}
_ALLOWED_UPDATE_FIELDS = {
    "api_endpoint",
    "api_key",
    "clear_api_key",
    "default_model",
    *_ROLE_FIELDS.values(),
}


@dataclass(frozen=True)
class ModelConnection:
    api_endpoint: str | None
    api_key: str | None = field(repr=False)


@dataclass(frozen=True)
class _Settings:
    api_endpoint: str | None
    default_model: str | None
    lead_model: str | None = None
    implementer_model: str | None = None
    verifier_model: str | None = None


class ModelProviderConfiguration:
    """Thread-safe, durable model settings with separately stored credentials."""

    def __init__(
        self,
        data_root: Path,
        *,
        bootstrap_model: str | None = None,
        bootstrap_api_endpoint: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.root = Path(data_root).expanduser().resolve()
        self.configuration_directory = self.root / "configuration"
        self.secret_directory = self.root / "secrets"
        self.settings_path = self.configuration_directory / "model-provider.json"
        self.api_key_path = self.secret_directory / "model-provider-api-key"
        self._lock = threading.RLock()
        self._environment = dict(os.environ if environment is None else environment)
        self._ensure_private_directory(self.root)
        self._ensure_private_directory(self.configuration_directory)
        self._ensure_private_directory(self.secret_directory)

        if self.settings_path.exists() or self.settings_path.is_symlink():
            self._settings = self._load_settings()
        else:
            self._settings = _Settings(
                api_endpoint=self._normalize_endpoint(
                    bootstrap_api_endpoint,
                    field_name="bootstrap API endpoint",
                ),
                default_model=self._normalize_bootstrap_model(bootstrap_model),
            )
        self._saved_api_key = self._load_saved_api_key()

    def public_state(self) -> dict[str, object]:
        with self._lock:
            model = self._settings.default_model
            source = self._api_key_source(model)
            api_key_required = (
                mini_swe.model_managed_api_key_variable(model) is not None
                if model is not None
                else False
            )
            return {
                "configured": (
                    model is not None and (not api_key_required or source is not None)
                ),
                "api_endpoint": self._settings.api_endpoint,
                "default_model": model,
                "lead_model": self._settings.lead_model,
                "implementer_model": self._settings.implementer_model,
                "verifier_model": self._settings.verifier_model,
                "api_key_configured": source is not None,
                "api_key_required": api_key_required,
                "api_key_source": source,
            }

    def update(self, values: Mapping[str, object]) -> dict[str, object]:
        if not isinstance(values, Mapping):
            raise ValueError("model configuration must be an object")
        unknown = sorted(set(values) - _ALLOWED_UPDATE_FIELDS)
        if unknown:
            raise ValueError(
                "unknown model configuration field(s): " + ", ".join(unknown)
            )
        with self._lock:
            current = self._settings
            default_model = self._updated_required_model(
                values,
                "default_model",
                current.default_model,
            )
            api_endpoint = self._updated_endpoint(values, current.api_endpoint)
            role_models = {
                role: self._updated_optional_model(
                    values,
                    field_name,
                    getattr(current, field_name),
                )
                for role, field_name in _ROLE_FIELDS.items()
            }
            replacement_key, clear_key = self._updated_api_key(values)
            if (
                replacement_key is not None
                and mini_swe.model_managed_api_key_variable(default_model) is None
            ):
                raise ValueError("model provider does not support a managed API key")
            new_settings = _Settings(
                api_endpoint=api_endpoint,
                default_model=default_model,
                lead_model=role_models["lead"],
                implementer_model=role_models["implementer"],
                verifier_model=role_models["verifier"],
            )
            self._write_settings(new_settings)
            if replacement_key is not None:
                self._atomic_write(self.api_key_path, replacement_key.encode("utf-8"))
            elif clear_key:
                self._remove_api_key_file()
            self._settings = new_settings
            if replacement_key is not None:
                self._saved_api_key = replacement_key
            elif clear_key:
                self._saved_api_key = None
            return self.public_state()

    def model_for_role(self, role: str) -> str:
        with self._lock:
            default_model = self._settings.default_model
            if default_model is None:
                raise RuntimeError(
                    "Model provider is not configured; save a default model in "
                    "the dashboard's Model provider settings."
                )
            field_name = _ROLE_FIELDS.get(role)
            if field_name is None:
                return default_model
            return getattr(self._settings, field_name) or default_model

    def connection_for_model(self, model: str) -> ModelConnection:
        normalized_model = self._normalize_model(model, "model")
        with self._lock:
            api_key = (
                self._saved_api_key
                if mini_swe.model_managed_api_key_variable(normalized_model) is not None
                else None
            )
            return ModelConnection(self._settings.api_endpoint, api_key)

    def model_catalog(self) -> dict[str, object]:
        with self._lock:
            endpoint = self._settings.api_endpoint
            model = self._settings.default_model
            api_key = self._saved_api_key
            if api_key is None and model is not None:
                api_key = self._environment_api_key(model)
        if endpoint is None:
            return {
                "available": False,
                "reason": "API endpoint missing",
                "models": [],
            }
        if api_key is None:
            return {
                "available": False,
                "reason": "API key missing",
                "models": [],
            }
        request = urllib.request.Request(
            f"{endpoint.rstrip('/')}/models",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=_MODEL_CATALOG_TIMEOUT,
            ) as response:
                body = response.read(_MAX_CATALOG_BYTES + 1)
            if len(body) > _MAX_CATALOG_BYTES:
                raise ValueError("response is too large")
            payload = json.loads(body.decode("utf-8"))
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise ValueError("response has no model list")
            identifiers = sorted(
                {
                    identifier.strip()
                    for item in data
                    if isinstance(item, dict)
                    and isinstance((identifier := item.get("id")), str)
                    and identifier.strip()
                    and len(identifier.strip()) <= _MAX_MODEL_LENGTH
                    and not self._has_control(identifier.strip())
                }
            )
        except urllib.error.HTTPError as error:
            return {
                "available": False,
                "reason": f"Model catalog unavailable (HTTP {error.code})",
                "models": [],
            }
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return {
                "available": False,
                "reason": "Model catalog unavailable",
                "models": [],
            }
        return {
            "available": True,
            "reason": None,
            "models": [
                {"id": identifier, "value": f"openai/{identifier}"}
                for identifier in identifiers
            ],
        }

    def default_inference_configuration(
        self,
    ) -> tuple[str, str | None, str | None]:
        model = self.model_for_role("default")
        connection = self.connection_for_model(model)
        return model, connection.api_endpoint, connection.api_key

    def _load_settings(self) -> _Settings:
        self._require_regular_file(self.settings_path, "model provider settings")
        try:
            raw = self.settings_path.read_text(encoding="utf-8")
            value = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"cannot load model provider settings: {error}"
            ) from error
        if not isinstance(value, dict) or value.get("version") != _SETTINGS_VERSION:
            raise RuntimeError("model provider settings have an unsupported format")
        allowed = {"version", "api_endpoint", "default_model", *_ROLE_FIELDS.values()}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise RuntimeError(
                "model provider settings contain unknown field(s): "
                + ", ".join(unknown)
            )
        try:
            default_model = self._normalize_model(
                value.get("default_model"),
                "default model",
            )
            api_endpoint = self._normalize_endpoint(
                value.get("api_endpoint"),
                field_name="API endpoint",
            )
            role_models = {
                role: self._normalize_optional_model(
                    value.get(field_name),
                    field_name.replace("_", " "),
                )
                for role, field_name in _ROLE_FIELDS.items()
            }
        except ValueError as error:
            raise RuntimeError(
                f"invalid saved model provider settings: {error}"
            ) from error
        self.settings_path.chmod(0o600)
        return _Settings(
            api_endpoint=api_endpoint,
            default_model=default_model,
            lead_model=role_models["lead"],
            implementer_model=role_models["implementer"],
            verifier_model=role_models["verifier"],
        )

    def _load_saved_api_key(self) -> str | None:
        if not self.api_key_path.exists() and not self.api_key_path.is_symlink():
            return None
        self._require_regular_file(self.api_key_path, "model provider API key")
        try:
            encoded = self.api_key_path.read_bytes()
            value = encoded.decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise RuntimeError(
                f"cannot load model provider API key: {error}"
            ) from error
        if not value or len(value) > _MAX_API_KEY_LENGTH or self._has_control(value):
            raise RuntimeError("saved model provider API key is invalid")
        self.api_key_path.chmod(0o600)
        return value

    def _write_settings(self, settings: _Settings) -> None:
        value = {
            "version": _SETTINGS_VERSION,
            "api_endpoint": settings.api_endpoint,
            "default_model": settings.default_model,
            "lead_model": settings.lead_model,
            "implementer_model": settings.implementer_model,
            "verifier_model": settings.verifier_model,
        }
        content = (
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        self._atomic_write(self.settings_path, content)

    def _remove_api_key_file(self) -> None:
        try:
            self.api_key_path.unlink()
        except FileNotFoundError:
            return
        self._sync_directory(self.secret_directory)

    @staticmethod
    def _require_regular_file(path: Path, description: str) -> None:
        try:
            metadata = path.lstat()
        except OSError as error:
            raise RuntimeError(f"cannot inspect {description}: {error}") from error
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"{description} must be a regular file")

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink() or not path.is_dir():
            raise RuntimeError(f"private state path must be a directory: {path}")
        path.chmod(0o700)

    @classmethod
    def _atomic_write(cls, path: Path, content: bytes) -> None:
        cls._ensure_private_directory(path.parent)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
            cls._sync_directory(path.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _updated_required_model(
        self,
        values: Mapping[str, object],
        field_name: str,
        current: str | None,
    ) -> str:
        value = values.get(field_name, current)
        return self._normalize_model(value, "default model")

    def _updated_optional_model(
        self,
        values: Mapping[str, object],
        field_name: str,
        current: str | None,
    ) -> str | None:
        if field_name not in values:
            return current
        return self._normalize_optional_model(
            values[field_name],
            field_name.replace("_", " "),
        )

    def _updated_endpoint(
        self,
        values: Mapping[str, object],
        current: str | None,
    ) -> str | None:
        if "api_endpoint" not in values:
            return current
        return self._normalize_endpoint(
            values["api_endpoint"], field_name="API endpoint"
        )

    @classmethod
    def _normalize_bootstrap_model(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return cls._normalize_model(value, "bootstrap model")

    @classmethod
    def _normalize_model(cls, value: object, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a nonempty string")
        normalized = value.strip()
        if len(normalized) > _MAX_MODEL_LENGTH or cls._has_control(normalized):
            raise ValueError(f"{field_name} is invalid or too long")
        return normalized

    @classmethod
    def _normalize_optional_model(
        cls,
        value: object,
        field_name: str,
    ) -> str | None:
        if value is None or value == "":
            return None
        return cls._normalize_model(value, field_name)

    @classmethod
    def _normalize_endpoint(
        cls,
        value: object,
        *,
        field_name: str,
    ) -> str | None:
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be a string")
        normalized = value.strip()
        if not normalized:
            return None
        if len(normalized) > _MAX_ENDPOINT_LENGTH or cls._has_control(normalized):
            raise ValueError(f"{field_name} is invalid or too long")
        parsed = urllib.parse.urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"{field_name} must be an absolute http or https URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(f"{field_name} must not contain credentials")
        if parsed.fragment:
            raise ValueError(f"{field_name} must not contain a fragment")
        try:
            parsed.port
        except ValueError as error:
            raise ValueError(f"{field_name} contains an invalid port") from error
        return normalized

    @classmethod
    def _updated_api_key(
        cls,
        values: Mapping[str, object],
    ) -> tuple[str | None, bool]:
        clear = values.get("clear_api_key", False)
        if not isinstance(clear, bool):
            raise ValueError("clear API key must be a boolean")
        raw = values.get("api_key")
        if raw is not None and not isinstance(raw, str):
            raise ValueError("API key must be a string")
        replacement = raw if isinstance(raw, str) and raw.strip() else None
        if replacement is not None:
            if len(replacement) > _MAX_API_KEY_LENGTH or cls._has_control(replacement):
                raise ValueError("API key is invalid or too long")
            if clear:
                raise ValueError("API key cannot be saved and cleared together")
        return replacement, clear

    def _api_key_source(self, model: str | None) -> str | None:
        if self._saved_api_key is not None:
            return "saved"
        if (
            model is not None
            and mini_swe.model_managed_api_key_variable(model) is not None
            and self._environment_api_key(model) is not None
        ):
            return "environment"
        return None

    def _environment_api_key(self, model: str) -> str | None:
        for variable in model_api_key_variables(model):
            value = self._environment.get(variable)
            if value:
                return value
        return None

    @staticmethod
    def _has_control(value: str) -> bool:
        return any(ord(character) < 32 or ord(character) == 127 for character in value)
