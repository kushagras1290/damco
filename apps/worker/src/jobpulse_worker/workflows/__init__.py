"""Temporal workflow definitions (deterministic; no I/O)."""

from jobpulse_worker.workflows.job_evaluation import JobEvaluationWorkflow
from jobpulse_worker.workflows.source_discovery import SourceDiscoveryWorkflow
from jobpulse_worker.workflows.source_polling import SourcePollingWorkflow

ALL_WORKFLOWS = [SourcePollingWorkflow, SourceDiscoveryWorkflow, JobEvaluationWorkflow]

__all__ = ["ALL_WORKFLOWS", "JobEvaluationWorkflow", "SourceDiscoveryWorkflow", "SourcePollingWorkflow"]
