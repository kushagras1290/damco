"""OpenAI-backed structured extraction and embeddings (configuration-driven models)."""

from jobpulse_core.intelligence.client import (
    EmbeddingOutcome,
    ExtractionOutcome,
    IntelligenceConfig,
    IntelligenceService,
    build_job_embedding_text,
    build_profile_embedding_text,
)

__all__ = [
    "EmbeddingOutcome",
    "ExtractionOutcome",
    "IntelligenceConfig",
    "IntelligenceService",
    "build_job_embedding_text",
    "build_profile_embedding_text",
]
