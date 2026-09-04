import os
import shutil
import tempfile
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def tmp_data_dir():
    d = Path(tempfile.mkdtemp(prefix="iqama_test_"))
    os.environ["IQAMA_DATA_DIR"] = str(d)
    os.environ["IQAMA_OCR_PROVIDER"] = os.environ.get("IQAMA_TEST_OCR_PROVIDER", "mock")
    os.environ["IQAMA_CONFIG_DIR"] = str(ROOT / "config")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="session")
def rules(tmp_data_dir):
    from app.engines.rules import RulesRepository
    return RulesRepository(ROOT / "config").active


@pytest.fixture
def clock():
    from app.core.clock import FixedClock
    return FixedClock(date(2026, 9, 4))


@pytest.fixture
def make_x():
    from app.engines.models import ExtractionResult, FieldValue

    def _mk(quality=0.8, conf=0.95, **kw):
        x = ExtractionResult(quality_score=quality, ocr_provider="mock")
        for k, v in kw.items():
            x.set(FieldValue(field=k, raw_text=v, normalized=v, confidence=conf))
        return x
    return _mk


@pytest.fixture(scope="session")
def api(tmp_data_dir):
    from fastapi.testclient import TestClient
    from app.api.deps import reset_deps
    from app.core.config import reset_settings
    from app.core.security import reset_encryptor
    from app.db.session import reset_db
    reset_settings(); reset_encryptor(); reset_db(); reset_deps()
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c
