import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Tests always run fully offline with mock providers and a throwaway DB.
_tmp = tempfile.mkdtemp(prefix="aitoy-test-")
os.environ.update({
    "LLM_PROVIDER": "mock", "STT_PROVIDER": "mock", "TTS_PROVIDER": "mock",
    "DB_PATH": str(Path(_tmp) / "test.db"),
    "HEARTBEAT_TIMEOUT_S": "2",
    "DEVICE_TOKEN": "",
})


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as c:
        yield c
