"""reviewflow - a deterministic multi-agent review framework on the OpenAI Agents SDK.

Public surface:

* :mod:`reviewflow.config` - environment-driven settings.
* :mod:`reviewflow.models.contracts` - the structured data contracts passed between stages.
* :mod:`reviewflow.orchestration.workflow` - the workflow engine (the entry point).
"""

from reviewflow.config import Settings
from reviewflow.orchestration.workflow import ReviewWorkflow

__all__ = ["ReviewWorkflow", "Settings", "__version__"]
__version__ = "0.1.0"
