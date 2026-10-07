"""Skill vocabulary and deterministic skill extraction (used when AI is unavailable)."""

from __future__ import annotations

import re
from functools import lru_cache

SKILL_SYNONYMS: dict[str, tuple[str, ...]] = {
    "python": ("python", "python3"),
    "fastapi": ("fastapi",),
    "django": ("django",),
    "flask": ("flask",),
    "pydantic": ("pydantic",),
    "sqlalchemy": ("sqlalchemy",),
    "postgresql": ("postgresql", "postgres", "psql"),
    "mysql": ("mysql",),
    "mongodb": ("mongodb", "mongo"),
    "redis": ("redis",),
    "kafka": ("kafka",),
    "temporal": ("temporal.io", "temporal workflows", "temporal"),
    "docker": ("docker", "containers"),
    "kubernetes": ("kubernetes", "k8s"),
    "terraform": ("terraform",),
    "aws": ("aws", "amazon web services"),
    "gcp": ("gcp", "google cloud"),
    "azure": ("azure",),
    "typescript": ("typescript", "ts"),
    "javascript": ("javascript", "js", "node.js", "nodejs"),
    "react": ("react", "react.js", "reactjs"),
    "next.js": ("next.js", "nextjs"),
    "go": ("golang", "go"),
    "rust": ("rust",),
    "java": ("java",),
    "llm": ("llm", "llms", "large language model", "large language models"),
    "rag": ("rag", "retrieval augmented generation", "retrieval-augmented generation"),
    "langchain": ("langchain",),
    "langgraph": ("langgraph",),
    "agentic ai": ("agentic ai", "ai agents", "agentic", "autonomous agents"),
    "openai": ("openai", "gpt-4", "gpt-5", "chatgpt"),
    "prompt engineering": ("prompt engineering",),
    "vector databases": ("vector database", "vector databases", "pgvector", "pinecone", "weaviate", "qdrant"),
    "machine learning": ("machine learning", "ml"),
    "deep learning": ("deep learning",),
    "pytorch": ("pytorch",),
    "tensorflow": ("tensorflow",),
    "nlp": ("nlp", "natural language processing"),
    "mlops": ("mlops",),
    "airflow": ("airflow",),
    "spark": ("spark", "pyspark"),
    "graphql": ("graphql",),
    "rest apis": ("rest api", "rest apis", "restful"),
    "microservices": ("microservices",),
    "ci/cd": ("ci/cd", "github actions", "continuous integration"),
    "observability": ("opentelemetry", "observability", "prometheus", "grafana"),
}


def normalize_skill(skill: str) -> str:
    lowered = skill.strip().lower()
    for canonical, aliases in SKILL_SYNONYMS.items():
        if lowered == canonical or lowered in aliases:
            return canonical
    return lowered


@lru_cache(maxsize=1)
def _patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for canonical, aliases in SKILL_SYNONYMS.items():
        # Very short ambiguous aliases ("go", "ts", "js", "ml") need word-ish boundaries
        # and are matched case-sensitively in their conventional casing to avoid noise.
        safe = [alias for alias in aliases if len(alias) > 2]
        short = [alias for alias in aliases if len(alias) <= 2]
        if safe:
            joined = "|".join(re.escape(alias) for alias in sorted(safe, key=len, reverse=True))
            compiled.append((canonical, re.compile(rf"(?<![\w.]){joined}(?![\w])", re.I)))
        if short:
            joined = "|".join(re.escape(alias.upper() if alias != "go" else "Go") for alias in short)
            compiled.append((canonical, re.compile(rf"(?<![\w.]){joined}(?![\w])")))
    return tuple(compiled)


def extract_skills(text: str) -> list[str]:
    """Canonical skills mentioned in ``text``, in vocabulary order."""
    return [canonical for canonical, pattern in _patterns() if pattern.search(text)]
