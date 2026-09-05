"""Run offline unit tests with an isolated PactFlow runtime workspace."""

import os
from pathlib import Path
import sys
import tempfile
import unittest


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    with tempfile.TemporaryDirectory(prefix="pactflow-regression-") as directory:
        os.environ["PACTFLOW_WORKSPACE"] = directory
        suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern=sys.argv[1] if len(sys.argv) > 1 else "test*.py")
        result = unittest.TextTestRunner(verbosity=1).run(suite)
        from pactflow.core.logger import audit_logger
        audit_logger.flush()
    sys.exit(0 if result.wasSuccessful() else 1)
