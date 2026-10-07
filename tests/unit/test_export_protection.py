"""P0-11: the permit export is an AES-256 encrypted ZIP that opens only with the returned password."""
import io

import pyzipper

from app.services.permit_export import PermitExportService


def test_protect_roundtrip():
    blob = PermitExportService._protect(b"PK\x03\x04hello", "permit_request_1.docx", "S3cretPassw0rdXY")
    with pyzipper.AESZipFile(io.BytesIO(blob)) as z:
        z.setpassword(b"S3cretPassw0rdXY")
        assert z.read("permit_request_1.docx") == b"PK\x03\x04hello"
    with pyzipper.AESZipFile(io.BytesIO(blob)) as z:
        z.setpassword(b"wrong")
        import pytest
        with pytest.raises(RuntimeError):
            z.read("permit_request_1.docx")
