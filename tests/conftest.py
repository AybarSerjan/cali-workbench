from __future__ import annotations

import os
import shutil
import tempfile
import uuid

import pytest
from fastapi.testclient import TestClient

_test_root = tempfile.mkdtemp(prefix="cali-tests-")
os.environ["CALI_WORKSPACE_ROOT"] = _test_root
os.environ["CALI_LLM_PROVIDER"] = "disabled"

from backend.main import app


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


@pytest.fixture
def unique_name():
    return f"test-{uuid.uuid4().hex[:10]}"


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_test_root, ignore_errors=True)
