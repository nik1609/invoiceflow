import os
import sys
import tempfile
from pathlib import Path

# Isolated data dir per test session. Set IF_TEST_TMPDIR to choose where it lives.
_tmp = tempfile.mkdtemp(prefix="invoiceflow_test_", dir=os.getenv("IF_TEST_TMPDIR") or None)
os.environ["IF_DATA_DIR"] = _tmp
os.environ["IF_STAGE_DELAY"] = "0"
os.environ["IF_NO_DOTENV"] = "1"
for _k in ("ANTHROPIC_API_KEY", "GROQ_API_KEY"):
    os.environ.pop(_k, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from scripts import gen_data, seed  # noqa: E402

gen_data.main()  # must exist at collection time: tests are parametrised from the manifest


@pytest.fixture()
def fresh():
    """Reset the database to the seeded state before each test."""
    seed.seed()
    yield
