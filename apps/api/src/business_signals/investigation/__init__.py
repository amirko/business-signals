"""Investigation workflow components.

The package separates graph orchestration from API transport, datasource adapters, prompt assets,
and optional external research.
"""

from business_signals.investigation.external_research import ExternalResearchCoordinator
from business_signals.investigation.workflow import InvestigationEngine

__all__ = ["ExternalResearchCoordinator", "InvestigationEngine"]
