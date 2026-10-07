"""Load an :class:`EligibilityPolicy` from the YAML format in the architecture doc."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError as PydanticValidationError

from jobpulse_core.domain.models import EligibilityPolicy
from jobpulse_core.errors import ConfigurationError

MAX_POLICY_BYTES = 64 * 1024


def load_policy_yaml(source: str | Path) -> EligibilityPolicy:
    """Parse YAML text or a path to a YAML file. ``safe_load`` only - never executes tags."""
    if isinstance(source, Path):
        if source.stat().st_size > MAX_POLICY_BYTES:
            raise ConfigurationError("policy file too large", context={"path": str(source)})
        text = source.read_text(encoding="utf-8")
    else:
        text = source
    try:
        payload = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigurationError("policy YAML is malformed") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("policy YAML must be a mapping")
    try:
        return EligibilityPolicy.model_validate(payload)
    except PydanticValidationError as exc:
        raise ConfigurationError("policy failed validation", context={"errors": exc.errors()}) from exc
