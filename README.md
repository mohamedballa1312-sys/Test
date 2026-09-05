# Iqama Screener — نظام فحص الإقامات السعودية وإنشاء ملف التصاريح

MVP (Phase 3). Screens Saudi Iqama card images with local OCR, applies configurable eligibility
rules (nationality → expiry → employer → occupation), routes anything uncertain to a human, and
produces a permit file (Excel / CSV).

> **Scope statement.** The system reads what is printed on the card. It is not an official
> verification, does not query government systems, and does not detect forgery. Every report
> carries this disclaimer.

Design documents: [`docs/01-requirements-analysis.md`](docs/01-requirements-analysis.md) ·
[`docs/01a-sample-analysis-findings.md`](docs/01a-sample-analysis-findings.md) ·
[`docs/02-architecture.md`](docs/02-architecture.md) · [`docs/03-mvp-notes.md`](docs/03-mvp-notes.md)

## Quick start (local, no Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pip install easyocr            # default provider (validated on 100 real cards). PaddleOCR is wired but untested: pip install paddlepaddle paddleocr; IQAMA_OCR_PROVIDER=paddle
cp .env.example .env           # optional; defaults work for a local trial

uvicorn app.main:app --port 8000                 # API  → http://127.0.0.1:8000/docs
streamlit run ui/streamlit_app.py                # UI   → http://127.0.0.1:8501
```

First run downloads OCR models once (EasyOCR: GitHub releases → `~/.EasyOCR`; PaddleOCR: Paddle hosts).
After that nothing leaves the machine: `ocr.external.enabled` is `false` and there is no external provider wired in.

## Docker

```bash
docker compose up --build       # api :8000, ui :8501, data in ./data, rules in ./config
```

## How a card flows

1. **Upload** (JPG/PNG/PDF, drag & drop, batch) → type sniffed by magic bytes, hashed for duplicates, stored encrypted.
2. **Preprocess** → card detected and perspective-cropped, upscaled, CLAHE; image quality score gate.
3. **OCR** (local) → label-anchored extraction that tolerates split/merged/garbled labels; digit-only
   second pass on numbers and dates; Luhn + prefix checks; RTL chunk-order resolution.
4. **Checks** ① nationality ② expiry (real current date, Asia/Riyadh) ③ employer (ID prefix `1/2`=individual,
   `7`=establishment; name only as a secondary signal) ④ occupation (allowlist; unlisted → review).
5. **Decision** → `REJECTED` (all reasons listed) / `MANUAL_REVIEW` (with recommendation) / `APPROVED`
   (only when `decision.auto_approve: true`; default off — a human approves).
6. **Review** → side-by-side image + fields, correct → engines re-run (no OCR), approve/reject, image deleted.
7. **Export** → `permit_file_<batch>.xlsx` / `.csv` (IDs masked unless explicitly unmasked, audited), and the
   **permit request** (Word/PDF) filled into the customer's own template — see `docs/05-permit-request.md`.

## Rules live in `config/`, not in code

| File | Purpose |
|---|---|
| `rules.yaml` | thresholds, check order, nationality mode, expiry warning days, auto-approve, retention |
| `occupations.csv` | occupation reference (Arabic/English/aliases/eligible) — editable from the UI |
| `nationalities.csv` | ISO code, Arabic/English names, aliases, approved flag |
| `employer_rules.yaml` | government / legal-form / activity keywords, person-name patterns |
| `employers_reference.csv` | known employers (ID or name → type) |
| `card_labels.yaml` | field labels as printed on the card + OCR variants |

Every file is schema-validated on load; an invalid file is rejected and the previous version stays active.
Each decision stores the rules version hash it was made with.

## API

`/docs` (OpenAPI). Main endpoints: `POST /api/v1/batches`, `POST /api/v1/batches/{id}/documents`,
`POST /api/v1/batches/{id}/process`, `GET /api/v1/batches/{id}` (progress + summary),
`GET /api/v1/batches/{id}/documents?decision=&trigger=&q=`, `GET /api/v1/documents/{id}`,
`PATCH /api/v1/documents/{id}/fields`, `POST /api/v1/documents/{id}/review`, `GET /api/v1/review/queue`,
`GET /api/v1/batches/{id}/export?format=xlsx|csv`, `GET|PUT /api/v1/rules/files/{name}`,
`POST /api/v1/rules/occupations`, `GET /api/v1/audit`. Headers: `X-Actor` (who), `X-API-Key` (if configured).

## Security & privacy (PDPL-minded)

- Local processing only; no external OCR/LLM unless a provider is explicitly wired and `ocr.external.enabled` is set.
- Sensitive columns (Iqama no., names, birth date, employer ID) and stored images are AES-256-GCM encrypted.
- ID numbers are masked in logs, API responses and exports by default; unmasking is an audited event.
- Images are deleted after a final decision (`retention.delete_images_after_final_decision`).
- Append-only audit log for every upload, processing, view, correction, review, export, rules change, deletion.

## Tests

```bash
pytest tests/unit                                     # engines, decision matrix, extraction on synthetic layouts
IQAMA_SAMPLES_DIR=/path/to/samples pytest tests/integration   # full API flow on real cards (never committed)
```

## Known limitations (MVP)

See `docs/03-mvp-notes.md`. In short: screenshot-quality input (~700 px) yields low-confidence reads that
are routed to review rather than decided; PaddleOCR is the intended production provider but was validated
here with EasyOCR because Paddle's model hosts were unreachable from the build sandbox.
