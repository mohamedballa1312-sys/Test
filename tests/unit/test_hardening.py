"""P0-01, P0-02, P0-09: insecure startup refused, API key enforced, stale documents re-queued."""



def test_startup_refuses_without_api_key(monkeypatch, tmp_data_dir):
    from app.core.config import Settings
    s = Settings(api_key=None, allow_insecure=False)
    assert any("IQAMA_API_KEY" in p for p in s.validate_for_startup())
    assert Settings(api_key="x" * 16, public_url="http://pilot.example").validate_for_startup()
    assert Settings(api_key="x" * 16, public_url="https://pilot.example").validate_for_startup() == []
    assert Settings(api_key=None, allow_insecure=True).validate_for_startup() == []


def test_api_key_required(api):
    assert api.get("/api/v1/batches").status_code == 200
    assert api.get("/api/v1/batches", headers={"X-API-Key": "wrong"}).status_code == 401
    assert api.get("/api/v1/batches", headers={"X-API-Key": ""}).status_code == 401
    assert api.get("/health").status_code == 200   # health stays unauthenticated for the load balancer


def test_upload_count_cap(api):
    bid = api.post("/api/v1/batches", json={"name": "cap"}).json()["id"]
    files = [("files", (f"f{i}.jpg", b"\xff\xd8\xff" + b"0" * 10, "image/jpeg")) for i in range(201)]
    assert api.post(f"/api/v1/batches/{bid}/documents", files=files).status_code == 413


def test_stale_processing_documents_are_requeued(api):
    from app.api.deps import batch_service
    from app.db.models import Batch, Document
    from app.db.session import session_scope
    with session_scope() as s:
        b = Batch(name="stale", total=1); s.add(b); s.flush()
        d = Document(batch_id=b.id, original_filename="x.jpg", sha256="s" * 64, mime="image/jpeg", status="PROCESSING")
        s.add(d); s.flush(); did = d.id
    svc = batch_service()
    calls = []
    svc.start_processing = lambda bid, actor: calls.append(bid)   # do not actually run OCR here
    assert svc.sweep_stale("test") == 1
    with session_scope() as s:
        assert s.get(Document, did).status == "QUEUED"
    assert calls and any(a["action"] == "DOCUMENT_REQUEUED" for a in api.get("/api/v1/audit").json())
