"""Pytest fixtures for ClaudeBar tests."""

import pytest
import tempfile
import json
import sys
from pathlib import Path

# Add src to path for imports
src_dir = Path(__file__).parent.parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))


@pytest.fixture
def temp_jsonl_file():
    """Create a temporary JSONL file with test entries."""
    def _create(entries):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            for entry in entries:
                f.write(json.dumps(entry) + '\n')
            return Path(f.name)
    return _create


@pytest.fixture
def temp_dir():
    """Create a temporary directory."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)
