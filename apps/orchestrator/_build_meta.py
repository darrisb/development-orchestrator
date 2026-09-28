"""Build identity, replaced at image build time.

The defaults here are the developer-build defaults: a source that does not know
what it was built from says so, rather than reporting a plausible-looking SHA
that nobody checked. `apps/orchestrator/services/deployment.py` is what reads
this, and `scripts/check_deployment_freshness.py` is what compares it to intent.
"""

SOURCE_REVISION = "unknown/dev"
SOURCE_DIRTY = None
BUILD_TIME = "unknown"
