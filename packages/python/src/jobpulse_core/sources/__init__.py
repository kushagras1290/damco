"""Source adapters and the factory that maps a :class:`SourceDefinition` to one."""

from jobpulse_core.sources.base import DiscoveryResult, JobSource
from jobpulse_core.sources.registry import build_source, required_hosts

__all__ = ["DiscoveryResult", "JobSource", "build_source", "required_hosts"]
