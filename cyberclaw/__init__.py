"""Backward-compatible import namespace for the PactFlow package."""

import pactflow as _pactflow

# Let Python resolve legacy submodules from the new package directory while
# keeping their historical import names intact.
__path__ = _pactflow.__path__
__all__: list[str] = []
