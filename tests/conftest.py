from pathlib import Path
from uuid import uuid4

import pytest

from research_agent.config import Settings
from research_agent.store import Store


@pytest.fixture
def workspace():
    # Explicit workspace-local fixtures avoid OS temporary-directory ACL differences.
    path = Path("artifacts/tests") / uuid4().hex
    path.mkdir(parents=True)
    return path


@pytest.fixture
def settings(workspace):
    return Settings(data_dir=workspace / "data")


@pytest.fixture
def store(settings):
    return Store(settings.data_dir)
