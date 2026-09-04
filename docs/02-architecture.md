# نظام فحص الإقامات السعودية — Phase 2: Architecture

| | |
|---|---|
| **الحالة** | مسودة للموافقة — لم يُكتب كود التطبيق بعد |
| **التاريخ** | 2026-09-04 |
| **يعتمد على** | `01-requirements-analysis.md` + `01a-sample-analysis-findings.md` + قرارات المستخدم (§0) |
| **المرحلة التالية** | Phase 3 — MVP (بعد موافقتك على هذه الوثيقة) |

---

## 0. القرارات المُثبَّتة — Rules v1

هذه إجاباتك على أسئلة Phase 1، وهي الآن **المصدر الرسمي للقواعد** حتى يصل ملف القواعد المكتوب (إن وُجد لاحقاً يُدمج ويُرفع الإصدار إلى v2).

| # | القرار | المصدر |
|---|---|---|
| D1 | **الفحوص أربعة بالترتيب:** ① الجنسية ② الصلاحية ③ صاحب العمل ④ المهنة | إجابتك 7 |
| D2 | **الجنسية:** بعض الجهات ترفض جنسيات معيّنة. التكوين يدعم: «كل الجنسيات معتمدة» أو قائمة جنسيات غير معتمدة | إجابتك 7 |
| D3 | **الانتهاء = سبب رفض أساسي** ولو كان الكفيل شركة أو مؤسسة | إجابتك 1، 2 |
| D4 | **قرب الانتهاء → مراجعة يدوية** مع إظهار الأيام المتبقية (العتبة قابلة للتكوين، افتراضياً 30 يوماً ⚠️ Q21) | إجابتك 6 |
| D5 | **كفالة فردية (بادئة 1 أو 2) → رفض مباشر** | إجابتك 3 |
| D6 | **مؤسسة = شركة** — كلاهما `COMPANY` (بادئة 7) | Phase 1 Q2 |
| D7 | **المهنة غير المدرجة → مراجعة** (نموذج Allowlist) | إجابتك 2 (الكتلة الأولى) |
| D8 | **كفالة مؤسسة/شركة → تذهب للمراجعة** | إجابتك 3 — ⚠️ **انظر Q20: هل يوجد قبول آلي أصلاً؟** |
| D9 | **QR غير متاح** (يعمل فقط للجهات المختصة) → **الاعتماد الكامل على OCR** | إجابتك 4 |
| D10 | **المدخل الإنتاجي:** غالباً لقطات شاشة من الجوال، وأحياناً صيغ أخرى | إجابتك 5 |
| D11 | التخطيط متغيّر بين إصدارات البطاقة → استخراج بالمرساة (label-anchored) | Phase 1a §3.2 |
| D12 | التواريخ ميلادية `YYYY/MM/DD` بأرقام عربية-هندية؛ لا هجري | Phase 1a §1 |

### ⚠️ Q20 — أهم سؤال معلّق في هذه المرحلة

قولك «كل ما هو كفالة مؤسسة أو شركة يذهب للمراجعة» يحتمل قراءتين:

| القراءة | المعنى | أثرها |
|---|---|---|
| **(أ) لا قبول آلي إطلاقاً** | النظام يرفض آلياً الحالات القاطعة فقط، وكل ما تبقّى يعتمده إنسان | كل بطاقة سارية بكفالة شركة تمر على مراجع — حتى لو مهنتها مدرجة كمؤهلة |
| **(ب) قبول آلي عند اكتمال الشروط** | إذا كانت الجنسية معتمدة + سارية + شركة + مهنة **مدرجة كمؤهلة** → `APPROVED` آلياً؛ وتذهب للمراجعة فقط عند الغموض | المراجع يرى الحالات الرمادية فقط |

**التصميم يدعم الاثنين بمفتاح واحد:** `decision.auto_approve: false | true`. **الافتراضي في هذه الوثيقة: (أ) `false`** — الأكثر أماناً والأقرب لصياغتك. في هذه الحالة يُرفق النظام بكل حالة مراجعة **توصية** (`RECOMMEND_APPROVE` / `NEEDS_ATTENTION`) ليكون اعتماد المراجع سريعاً. **أرجو تأكيد (أ) أو (ب).**

---

## 1. المعمارية العامة

### 1.1 المبدأ: Modular Monolith

تطبيق واحد قابل للنشر (سهولة التشغيل المحلي) لكنه مقسّم داخلياً إلى وحدات بحدود صارمة، بحيث يمكن فصل أي وحدة إلى خدمة مستقلة لاحقاً دون إعادة كتابة.

```
┌────────────────────────────────────────────────────────────────────┐
│  UI (Streamlit — MVP)                                              │
│  Upload · Progress · Dashboard · Review Queue · Rules Admin        │
└──────────────────────────────┬─────────────────────────────────────┘
                               │ HTTP/JSON
┌──────────────────────────────▼─────────────────────────────────────┐
│  API — FastAPI                                                     │
│  /batches  /documents  /review  /export  /rules  /audit            │
│  Auth · Validation · PII Masking · Audit Hook                      │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
┌──────────────────────────────▼─────────────────────────────────────┐
│  SERVICES                                                          │
│                                                                    │
│  BatchService ──► PipelineRunner (worker pool)                     │
│                       │                                            │
│    ┌──────────────────▼──────────────────┐                         │
│    │  pipeline/                          │                         │
│    │   ingest → preprocess → ocr →       │                         │
│    │   extract → validate                │                         │
│    └──────────────────┬──────────────────┘                         │
│                       │ ExtractionResult                           │
│    ┌──────────────────▼──────────────────┐                         │
│    │  engines/                           │                         │
│    │   ① nationality  ② expiry           │                         │
│    │   ③ employer     ④ occupation       │                         │
│    │   → rules → decision                │                         │
│    └──────────────────┬──────────────────┘                         │
│                       │ Decision                                   │
│         ┌─────────────┴─────────────┐                              │
│   ReviewService               ReportService                        │
│   (queue, correct, re-decide)  (xlsx / csv / pdf)                  │
└───────┬───────────────┬───────────────┬────────────────────────────┘
        │               │               │
   ┌────▼────┐    ┌─────▼─────┐   ┌─────▼──────┐
   │  DB     │    │  Config   │   │  Audit Log │
   │ SQLite→ │    │ rules.yaml│   │ append-only│
   │ Postgres│    │ *.csv     │   │            │
   └─────────┘    └───────────┘   └────────────┘
```

### 1.2 حدود الوحدات — قواعد الاعتماد

| الوحدة | تعتمد على | **لا تعتمد على** |
|---|---|---|
| `pipeline/` | OpenCV, OCRProvider | engines, db, api |
| `engines/` | `RulesRepository` (قراءة فقط) | pipeline, db, api |
| `services/` | pipeline, engines, db | api, ui |
| `api/` | services | pipeline, engines مباشرةً |
| `ui/` | api (HTTP فقط) | أي شيء آخر |

**النتيجة:** المحركات قابلة للاختبار بمعزل عن OCR وقاعدة البيانات؛ والواجهة قابلة للاستبدال بـ Next.js دون لمس أي منطق.

### 1.3 هيكل المشروع

```
iqama-screener/
├── app/
│   ├── main.py                    # FastAPI app factory
│   ├── core/
│   │   ├── config.py              # إعدادات التشغيل (env)
│   │   ├── security.py            # تشفير، تجزئة، إخفاء PII
│   │   ├── logging.py             # structlog + معالج الإخفاء
│   │   └── clock.py               # مصدر الوقت الوحيد (Asia/Riyadh) — قابل للحقن في الاختبارات
│   ├── pipeline/
│   │   ├── ingest.py              # قبول الملفات، magic bytes، PDF→صور
│   │   ├── preprocess.py          # قصّ، تصحيح، تحسين، quality score
│   │   ├── ocr/
│   │   │   ├── base.py            # OCRProvider (interface) + OCRLine
│   │   │   ├── paddle.py          # LocalProvider
│   │   │   └── vision_llm.py      # اختياري — معطّل افتراضياً
│   │   ├── extract.py             # label-anchored field extraction
│   │   ├── normalize.py           # أرقام، عربي، تواريخ، جنسيات
│   │   └── validate.py            # checksum، منطق التواريخ، اكتمال
│   ├── engines/
│   │   ├── nationality.py         # ① 
│   │   ├── expiry.py              # ②
│   │   ├── employer.py            # ③ L0 prefix → L1..L5
│   │   ├── occupation.py          # ④ allowlist + fuzzy
│   │   ├── rules.py               # RulesRepository: تحميل/تحقق/إصدار
│   │   └── decision.py            # البوابات G0..G3
│   ├── services/
│   │   ├── batch.py               # إنشاء دفعة، تشغيل، تقدّم
│   │   ├── review.py              # طابور، تصحيح، إعادة قرار
│   │   ├── report.py              # xlsx / csv / pdf
│   │   └── retention.py           # حذف الصور والبيانات حسب السياسة
│   ├── db/
│   │   ├── models.py              # SQLAlchemy
│   │   ├── repo.py                # استعلامات
│   │   └── migrations/            # Alembic
│   ├── audit/
│   │   └── log.py                 # append-only
│   └── api/
│       ├── batches.py  documents.py  review.py
│       ├── export.py   rules.py      audit.py
│       └── schemas.py             # Pydantic I/O
├── config/
│   ├── rules.yaml                 # العتبات ومعايير القرار
│   ├── nationalities.csv          # ① 
│   ├── occupations.csv            # ④
│   ├── employer_rules.yaml        # ③ بادئات + كلمات مفتاحية
│   ├── employers_reference.csv    # ③ أصحاب عمل معروفون (L1)
│   └── card_labels.yaml           # تسميات الحقول على البطاقة + مرادفات OCR
├── ui/
│   └── streamlit_app.py
├── tests/
│   ├── unit/                      # engines بمعزل
│   ├── integration/               # pipeline على عيّنات مموّهة
│   └── fixtures/                  # ExtractionResult جاهزة (بدون صور حقيقية)
├── docker-compose.yml
└── pyproject.toml
```

---

## 2. تدفق البيانات

### 2.1 المسار الرئيسي

```
[1] رفع ملفات ──► ingest
      • فحص magic bytes، الحجم، العدد
      • PDF → صور (صفحة/صورة)
      • hash للكشف عن التكرار
      • تخزين مؤقت مشفّر + سجل Document(status=QUEUED)

[2] PipelineRunner (worker pool, N قابل للتكوين)
      لكل Document:
      preprocess ──► quality_score
          │ < threshold ──► Decision(MANUAL_REVIEW, trigger=POOR_IMAGE) ──► [5]
          ▼
      ocr (OCRProvider) ──► [OCRLine{text, bbox, conf}]
          ▼
      extract (anchored) ──► {field: FieldValue{raw, bbox, conf}}
          ▼
      normalize + validate ──► ExtractionResult
          ▼
[3] engines (كلها تعمل، بالترتيب D1)
      ① nationality ──► CheckResult
      ② expiry      ──► CheckResult
      ③ employer    ──► CheckResult
      ④ occupation  ──► CheckResult
          ▼
      decision(G0→G3) ──► Decision{status, reasons[], triggers[], recommendation}
          ▼
[4] حفظ: extracted_fields + check_results + decision  (مشفّر)
      audit: DOCUMENT_PROCESSED

[5] إن MANUAL_REVIEW ──► review queue
      مراجع يصحّح حقولاً ──► إعادة تشغيل [3] فقط على القيم المصحّحة
      ──► Decision جديد (version+1) ──► audit: REVIEW_SUBMITTED

[6] اكتمال الدفعة ──► ReportService ──► xlsx/csv/pdf
      audit: REPORT_EXPORTED
      retention: حذف الصور (افتراضياً فوراً بعد الاعتماد النهائي)
```

### 2.2 ما يُعاد تشغيله عند التصحيح اليدوي

فقط **[3]** — المحركات والقرار. لا OCR، لا معالجة مسبقة. القيم المصحّحة تُعلَّم `source=manual, confidence=1.0`. القرار الجديد يُخزَّن كإصدار جديد ولا يمحو السابق.

---

## 3. خط الأنابيب — OCR والاستخراج

### 3.1 المعالجة المسبقة

| الخطوة | الأداة | ملاحظة خاصة بلقطات الشاشة (D10) |
|---|---|---|
| كشف البطاقة وقصّها | كشف أكبر مستطيل فاتح ذي زوايا مدوّرة | لقطات الشاشة تحوي عناصر واجهة (أزرار، أيقونات) خارج البطاقة — القصّ يزيلها |
| تصحيح المنظور | `cv2.getPerspectiveTransform` | لقطات الشاشة مستوية غالباً؛ صور الكاميرا تحتاجه |
| الدوران | اختبار 0/90/180/270 على قوة النص | |
| التحسين | CLAHE على قناة L | |
| رفع الدقة | `cv2.resize` ×2 (LANCZOS) إذا كان عرض البطاقة < 1200px | العيّنة 1 (704px) تحتاجه |
| **Quality Score** | تباين لابلاس (وضوح) + عرض البطاقة + نسبة الوهج | العتبة في `rules.yaml` |

### 3.2 واجهة OCR

```python
@dataclass
class OCRLine:
    text: str
    bbox: tuple[int, int, int, int]   # x, y, w, h
    confidence: float                 # 0..1

class OCRProvider(Protocol):
    name: str
    def read(self, image: np.ndarray) -> list[OCRLine]: ...
```

`LocalProvider` = PaddleOCR (`lang="arabic"`) — الافتراضي. `VisionLLMProvider` موجود كهيكل لكنه معطّل ويتطلب `ocr.external.enabled: true` + إقرار. **لا شيء يغادر الجهاز في MVP.**

### 3.3 الاستخراج بالمرساة — الخوارزمية

البطاقة: **التسمية يميناً ← `:` ← القيمة يسارها**، على نفس السطر البصري.

```
1. لكل OCRLine، حاول مطابقتها ضبابياً مع تسميات card_labels.yaml
   (RapidFuzz ratio ≥ 0.80 بعد التطبيع) → إن طابقت: هي "مرساة" لحقل معيّن.
2. لكل مرساة: القيمة = اتحاد كل OCRLine غير-مرساة تقع:
     • على نفس السطر (تداخل عمودي ≥ 50%)
     • وإلى يسار المرساة (x + w ≤ anchor.x)
     • وأقرب من المرساة التالية على اليسار (إن وُجدت — الصفوف ذات حقلين)
3. الاسم العربي/الإنجليزي: أكبر سطرين ارتفاعاً فوق أول مرساة.
4. حقول الأنماط (تعمل حتى لو فشلت المرساة):
     • رقم الهوية:        [٠-٩0-9]{10} يبدأ بـ 2
     • هوية صاحب العمل:   [٠-٩0-9]{10} يبدأ بـ 1|2|7
     • التواريخ:          [٠-٩0-9]{4}/[٠-٩0-9]{2}/[٠-٩0-9]{2}
   عند تعارض النمط مع المرساة → خفض الثقة.
5. confidence(field) = ocr_conf(value) × label_match_score
```

**لماذا يصمد أمام تباين التخطيط (D11):** لا يوجد افتراض عن عدد الصفوف أو موقعها — الحقل الغائب (`مكان العمل` في العيّنة 1) ببساطة لا يجد مرساة ويبقى `None`.

### 3.4 التطبيع

| الحقل | التحويل |
|---|---|
| الأرقام | `٠١٢٣٤٥٦٧٨٩ → 0123456789`، ثم تصحيح O→0، l/I→1، S→5 داخل الحقول الرقمية فقط |
| التواريخ | `YYYY/MM/DD` → `date` — رفض أي تاريخ لا يُحلَّل |
| العربي | إزالة التشكيل والتطويل؛ `أإآ→ا`؛ `ة→ه`؛ `ى→ي`؛ توحيد المسافات |
| الجنسية | جدول مرادفات → ISO 3166-1 alpha-2 (`السودان`→`SD`) |
| صاحب العمل (الاسم) | تطبيع عربي فقط — يُحفظ الخام دائماً |

### 3.5 التحقق

- رقم الهوية: 10 أرقام، يبدأ بـ 2، Luhn (إشارة ترجيحية — يخفض الثقة ولا يرفض).
- هوية صاحب العمل: 10 أرقام، البادئة ∈ {1, 2, 7} وإلا `UNKNOWN`.
- تاريخ الميلاد < تاريخ الانتهاء؛ تاريخ الميلاد ضمن [1920, اليوم−15 سنة].
- **الحقول الحرجة** (يجب توفّرها بثقة ≥ العتبة وإلا `MANUAL_REVIEW`): رقم الهوية، تاريخ الانتهاء، الجنسية، هوية صاحب العمل، المهنة.

---

## 4. المحركات الأربعة

كل محرك يُنتج نفس البنية:

```python
@dataclass
class CheckResult:
    check: str                  # NATIONALITY | EXPIRY | EMPLOYER | OCCUPATION
    outcome: str                # PASS | FAIL | REVIEW | UNKNOWN
    label: str                  # القيمة المصنَّفة (e.g. INDIVIDUAL, EXPIRED)
    confidence: float
    reason: str | None          # نص سبب الرفض/المراجعة (يظهر في التقرير)
    evidence: list[dict]        # سلسلة الأدلة
    details: dict               # حقول إضافية (days_remaining ...)
    rules_version: str
```

### ① الجنسية — `nationality.py`

```
input : nationality_code (ISO) أو None
config: nationalities.csv + rules.yaml:nationality.mode

mode = ALL_APPROVED      → PASS دائماً (إلا إن لم تُقرأ → REVIEW)
mode = BLOCKLIST         → code ∈ not_approved ? FAIL("Nationality not approved: X") : PASS
mode = ALLOWLIST         → code ∈ approved ? PASS : FAIL
غير مقروءة / غير معروفة → REVIEW("Nationality unreadable")
```

### ② الصلاحية — `expiry.py`

```
today = clock.today()                      # Asia/Riyadh — لا تاريخ ثابت
days  = (expiry - today).days
days < 0                     → FAIL  (EXPIRED, "Iqama Expired (YYYY-MM-DD, -N days)")
0 ≤ days ≤ warn_days         → REVIEW(EXPIRING_SOON, "Expires in N days")     ← D4
days > warn_days             → PASS  (VALID)
expiry is None               → REVIEW(DATE_NOT_READABLE)
details = {expiry, check_date, days_remaining}
```

### ③ صاحب العمل — `employer.py`

```
L0  employer_id prefix:  1|2 → INDIVIDUAL (0.99)   7 → COMPANY (0.99)      ← القاعدة الحتمية
L1  employers_reference.csv (اسم مطبَّع أو رقم) → التصنيف المخزّن (1.00)
L2  كلمات حكومية   → GOVERNMENT (0.95)
L3  صيغة نظامية    → COMPANY (0.90)
L4  مؤشر نشاط      → COMPANY (0.75)
L5  اسم شخص        → INDIVIDUAL (0.60) — لا يكفي وحده للرفض
else               → UNKNOWN

تحقق ثانوي: إن كانت L0 = COMPANY لكن الاسم يطابق L5 فقط (أو العكس)
            → confidence −0.30 → يسقط تحت عتبة الرفض الآلي → REVIEW

outcome:
  INDIVIDUAL & conf ≥ individual_auto_reject_threshold → FAIL ("Individual Employer")   ← D5
  INDIVIDUAL & conf <  threshold                        → REVIEW
  COMPANY | GOVERNMENT                                  → PASS
  UNKNOWN                                               → REVIEW
```

### ④ المهنة — `occupation.py` (Allowlist — D7)

```
norm = normalize_ar(occupation_raw)
1. exact match  في occupations.csv          → conf 1.00
2. alias match                               → conf 0.95
3. fuzzy (RapidFuzz token_set_ratio ≥ 0.88)  → conf = score
4. لا مطابقة                                  → UNKNOWN

outcome:
  matched & eligible = No     → FAIL   ("Excluded Occupation: <EN> (<AR>)")
  matched & eligible = Yes    → PASS
  matched & conf < 0.95       → REVIEW ("Fuzzy occupation match: X ≈ Y")
  UNKNOWN                     → REVIEW ("Occupation not in reference list")   ← D7
```

---

## 5. محرك القواعد — `rules.py`

### 5.1 المسؤوليات

1. تحميل كل ملفات `config/` عند الإقلاع وعند الطلب (hot reload).
2. **التحقق من الصحة بـ Pydantic schema** — ملف معطوب يُرفض ويبقى الإصدار السابق فعّالاً.
3. حساب `rules_version = sha256(كل الملفات)[:12]` وتخزينه مع كل قرار.
4. حفظ نسخة من كل إصدار في `rules_versions` للرجوع والتدقيق.
5. إتاحة القراءة للمحركات عبر واجهة واحدة `RulesRepository` (قراءة فقط).

### 5.2 `config/rules.yaml`

```yaml
version_note: "Rules v1 — from user decisions 2026-09-04"

image:
  min_quality_score: 0.45
  min_card_width_px: 600
  upscale_below_px: 1200

ocr:
  provider: local                  # local | vision_llm | consensus
  min_field_confidence: 0.75       # الحقول الحرجة
  critical_fields: [iqama_no, expiry_date, nationality, employer_id, occupation]
  external:
    enabled: false                 # يتطلب إقراراً صريحاً — انظر Phase 1 §10
    acknowledged_by: null

checks:
  order: [NATIONALITY, EXPIRY, EMPLOYER, OCCUPATION]     # D1

nationality:
  mode: ALL_APPROVED               # ALL_APPROVED | BLOCKLIST | ALLOWLIST   ← D2
  file: nationalities.csv

expiry:
  timezone: Asia/Riyadh
  warn_days: 30                    # ⚠️ Q21 — EXPIRING_SOON → REVIEW (D4)

employer:
  individual_auto_reject_threshold: 0.85       # D5 — L0 تتجاوزها، L5 لا
  id_prefix_map: { "1": INDIVIDUAL, "2": INDIVIDUAL, "7": COMPANY }
  rules_file: employer_rules.yaml
  reference_file: employers_reference.csv

occupation:
  model: ALLOWLIST                 # D7 — غير المدرج → REVIEW
  fuzzy_threshold: 0.88
  review_below_confidence: 0.95
  file: occupations.csv

decision:
  auto_approve: false              # ⚠️ Q20 — (أ)=false  (ب)=true
  require_human_confirmation_on_reject: false
  hard_fail_min_confidence: 0.85   # رفض آلي فقط إن كانت ثقة الفحص الراسب ≥ هذا

retention:
  delete_images_after_final_decision: true
  data_retention_days: 365
```

### 5.3 `config/occupations.csv`

```csv
code,occupation_ar,occupation_en,category,eligible,reason,aliases,updated_by,updated_at
,طباخ,Cook,Individual/Excluded,No,Excluded occupation,"طبّاخ|شيف|معلم طبخ",admin,2026-09-04
,حلاق,Barber,Individual/Excluded,No,Excluded occupation,"مزين|حلاق رجالي",admin,2026-09-04
,راعي مواشي,Livestock Herder,Individual/Excluded,No,Excluded occupation,"راعي غنم|راعي إبل|راعي",admin,2026-09-04
,سائق خاص,Private Driver,Individual/Excluded,No,Excluded occupation,"سائق منزل|سائق عائلة",admin,2026-09-04
```
`code` (SSCO) اختياري ويُملأ لاحقاً. **لا توجد مهن مؤهلة بعد** — بموجب D7 كل مهنة أخرى → مراجعة، والمراجع يستطيع إضافتها كمؤهلة من الواجهة فتصبح `PASS` تلقائياً في المرات القادمة.

### 5.4 `config/nationalities.csv`

```csv
code,name_ar,name_en,aliases,approved,note,updated_by,updated_at
SD,السودان,Sudan,"سوداني|السودانية",Yes,,admin,2026-09-04
```
يُملأ من قائمة ISO كاملة في Phase 3. مع `mode: ALL_APPROVED` لا يُستخدم عمود `approved`؛ مع `BLOCKLIST` تُرفض الصفوف `approved=No`.

### 5.5 `config/employer_rules.yaml`

```yaml
government_keywords: [وزارة, هيئة, أمانة, بلدية, جامعة, مستشفى, إمارة, المديرية العامة]
legal_form_keywords: [شركة, مؤسسة, مصنع, مجموعة, ذ.م.م, مساهمة, تضامن, co, ltd, llc, est, group]
activity_keywords: [للتجارة, للمقاولات, للخدمات, القابضة, للصناعة, للنقل]
person_name_patterns:
  - "^\\S+ \\S+ \\S+( \\S+)?$"      # 3–4 كلمات بدون أي كلمة مفتاحية
  - "\\b(بن|بنت)\\b"
```

### 5.6 `config/card_labels.yaml`

```yaml
iqama_no:       ["رقم الهوية"]
expiry_date:    ["تاريخ الانتهاء", "تاريخ الإنتهاء"]
birth_date:     ["تاريخ الميلاد"]
birth_place:    ["مكان الميلاد"]
nationality:    ["الجنسية"]
religion:       ["الديانة"]
occupation:     ["المهنة"]
employer_id:    ["هوية صاحب العمل"]
issue_place:    ["مكان الإصدار", "مكان الاصدار"]
work_place:     ["مكان العمل"]            # اختياري — غائب في بعض الإصدارات
employer_name:  ["اسم صاحب العمل"]
version_no:     ["رقم النسخة"]
```

---

## 6. محرك القرار — `decision.py`

### 6.1 البوابات

```
G0  جودة/اكتمال
    quality < min  أو  أي حقل حرج مفقود أو ثقته < min_field_confidence
    → MANUAL_REVIEW, triggers += [POOR_IMAGE | LOW_CONFIDENCE:<field>]

G1  رفض قاطع (بترتيب D1 — تُجمع كل الأسباب، لا يتوقف عند الأول)
    لكل CheckResult: outcome == FAIL and confidence ≥ hard_fail_min_confidence
    → REJECTED, reasons = [كل أسباب FAIL بالترتيب]
    (إن كان require_human_confirmation_on_reject → MANUAL_REVIEW + recommendation=RECOMMEND_REJECT)

G2  مراجعة
    أي CheckResult.outcome ∈ {REVIEW, UNKNOWN}  أو  FAIL بثقة منخفضة
    → MANUAL_REVIEW, triggers = [الأسباب]

G3  اكتمال
    كل الفحوص PASS:
      auto_approve == true   → APPROVED
      auto_approve == false  → MANUAL_REVIEW, recommendation = RECOMMEND_APPROVE     ← D8/Q20
```

### 6.2 المخرج

```python
@dataclass
class Decision:
    status: str                     # APPROVED | REJECTED | MANUAL_REVIEW
    reasons: list[str]              # للرفض — مرقّمة بترتيب D1
    review_triggers: list[str]      # للمراجعة
    recommendation: str | None      # RECOMMEND_APPROVE | RECOMMEND_REJECT | NEEDS_ATTENTION
    checks: list[CheckResult]
    rules_version: str
    decided_at: datetime
    decided_by: str                 # "system" | user_id
    version: int                    # يزيد مع كل إعادة قرار
```

### 6.3 مصفوفة القرار v2 (تطبيقاً على العيّنات الثلاث، `auto_approve=false`)

| العيّنة | ① جنسية | ② صلاحية | ③ كفيل | ④ مهنة | القرار | التوصية/الأسباب |
|---|---|---|---|---|---|---|
| 1 | PASS | **FAIL** −1363 | **FAIL** INDIVIDUAL | **FAIL** سائق خاص | **REJECTED** | 1. Expired · 2. Individual Employer · 3. Excluded: Private Driver |
| 2 | PASS | PASS +117 | PASS COMPANY | REVIEW (غير مدرجة) | **MANUAL_REVIEW** | NEEDS_ATTENTION: Occupation not in list |
| 3 | PASS | **FAIL** −155 | PASS COMPANY | REVIEW | **REJECTED** | 1. Expired *(الرفض القاطع يتقدّم على المراجعة — D3)* |

---

## 7. مخطط قاعدة البيانات

```
users              id, username, role(OPERATOR|REVIEWER|RULES_ADMIN|AUDITOR), pw_hash, active, created_at

batches            id, name, created_by→users, created_at, status(QUEUED|PROCESSING|DONE|FAILED),
                   total, processed, rules_version

documents          id, batch_id→batches, original_filename, sha256, mime, page_no,
                   image_path(nullable — يُحذف حسب السياسة), image_deleted_at,
                   quality_score, status(QUEUED|PROCESSING|DONE|ERROR), error_msg,
                   ocr_provider, processed_at

extracted_fields   id, document_id→documents, field_name, raw_text, normalized_value,
                   confidence, bbox_x, bbox_y, bbox_w, bbox_h,
                   source(OCR|MANUAL|DERIVED), corrected_by→users, corrected_at,
                   is_current(bool)                       ← يُحفظ تاريخ التصحيحات
                   [normalized_value مشفّر لحقول: iqama_no, name_ar, name_en, birth_date, employer_id]
                   [iqama_no_hash — للبحث والتكرار دون فك التشفير]

check_results      id, decision_id→decisions, check(NATIONALITY|EXPIRY|EMPLOYER|OCCUPATION),
                   outcome, label, confidence, reason, evidence_json, details_json

decisions          id, document_id→documents, version, status, reasons_json, triggers_json,
                   recommendation, rules_version, decided_by, decided_at, is_current

reviews            id, document_id, reviewer_id→users, started_at, submitted_at,
                   final_status, note, previous_decision_id, new_decision_id

rules_versions     version(hash), loaded_at, loaded_by, files_snapshot_json, is_active

reference_occupations / reference_nationalities / reference_employers
                   نسخة DB من ملفات CSV (للتحرير من الواجهة) — الملف يبقى المصدر القابل للتصدير

audit_log          id, ts, actor, action, entity_type, entity_id, details_json(PII مُخفى), ip
                   [append-only: لا UPDATE ولا DELETE على مستوى الصلاحيات]
```

**التشفير:** AES-256-GCM على مستوى العمود عبر `SQLAlchemy TypeDecorator`؛ المفتاح من متغير بيئة / ملف مفاتيح خارج المستودع. **الفهارس:** `documents.sha256`, `extracted_fields(document_id, field_name, is_current)`, `decisions(document_id, is_current)`, `iqama_no_hash`.

---

## 8. تصميم الـ API

كل النقاط تحت `/api/v1`. الاستجابات JSON، الأخطاء بصيغة `{error, detail}`. رقم الهوية يُعاد **مُخفى** (`2401******`) إلا للأدوار المصرّح لها مع تسجيل الكشف في التدقيق.

| Method | Path | الوظيفة | الدور |
|---|---|---|---|
| POST | `/batches` | إنشاء دفعة `{name}` | OPERATOR |
| POST | `/batches/{id}/documents` | رفع ملفات (multipart, متعدد) | OPERATOR |
| POST | `/batches/{id}/process` | بدء المعالجة (خلفية) | OPERATOR |
| GET | `/batches/{id}` | الحالة + التقدّم `processed/total` + الملخّص | الجميع |
| GET | `/batches/{id}/documents?decision=&trigger=&q=` | القائمة مع فلاتر (drill-down) | الجميع |
| GET | `/documents/{id}` | الحقول + الفحوص + القرار الحالي + التاريخ | الجميع |
| GET | `/documents/{id}/image` | الصورة (إن لم تُحذف) — مُسجَّل في التدقيق | REVIEWER |
| PATCH | `/documents/{id}/fields` | تصحيح حقول `{field: value}` → إعادة قرار | REVIEWER |
| POST | `/documents/{id}/review` | اعتماد نهائي `{status, note}` | REVIEWER |
| GET | `/review/queue?sort=trigger` | طابور المراجعة | REVIEWER |
| GET | `/batches/{id}/export?format=xlsx\|csv\|pdf&unmask=false` | ملف التصاريح | OPERATOR |
| GET | `/rules` | القواعد الفعّالة + الإصدار | الجميع |
| PUT | `/rules/{file}` | رفع ملف قواعد جديد → تحقق → تفعيل | RULES_ADMIN |
| POST | `/rules/reload` | إعادة تحميل من القرص | RULES_ADMIN |
| GET | `/rules/versions` | السجل | AUDITOR |
| POST | `/rules/occupations` | إضافة/تعديل مهنة من الواجهة | REVIEWER+ |
| GET | `/audit?from=&to=&actor=&action=` | سجل التدقيق | AUDITOR |
| DELETE | `/documents/{id}` · `/batches/{id}` | حذف كامل (بيانات + صور) | OPERATOR+ |
| GET | `/health` | | — |

**المصادقة (MVP):** مستخدم واحد + مفتاح جلسة. **Phase 5:** JWT + RBAC كامل.

---

## 9. الأمان — التنفيذ

| الضابط | الآلية |
|---|---|
| إخفاء PII في السجلات | `structlog` processor يطبّق regex على كل حقل: `\b2\d{9}\b → 2******` قبل الكتابة |
| تشفير الأعمدة | `EncryptedString` TypeDecorator، AES-256-GCM، مفتاح من `IQAMA_ENC_KEY` |
| البحث دون فك تشفير | `iqama_no_hash = HMAC-SHA256(key, iqama_no)` |
| الصور | تُخزَّن مشفّرة على القرص (`cryptography.Fernet`)، تُحذف حسب `retention` |
| رفع الملفات | `python-magic` على المحتوى لا الامتداد؛ حد حجم؛ رفض التنفيذيات |
| لا شبكة خارجية | `ocr.external.enabled=false` افتراضياً؛ لا مكتبة تتصل بالإنترنت وقت التشغيل |
| التدقيق | كل عملية عبر الـ API تمر على middleware يكتب `audit_log` |

---

## 10. Technology Stack — النهائي

| الطبقة | الاختيار | الإصدار |
|---|---|---|
| Python | 3.11+ | |
| API | FastAPI + Uvicorn | |
| Validation | Pydantic v2 | |
| CV | opencv-python-headless, Pillow | |
| OCR | PaddleOCR (`lang=arabic`) + PaddlePaddle CPU | |
| Text | RapidFuzz, pyarabic | |
| DB | SQLite (MVP) → PostgreSQL · SQLAlchemy 2 · Alembic | |
| Crypto | cryptography | |
| Logging | structlog | |
| Excel/CSV | openpyxl, pandas | |
| PDF | WeasyPrint | Phase 5 |
| UI | Streamlit (MVP) → Next.js (Phase 5) | |
| Batch | FastAPI BackgroundTasks + ThreadPool (MVP) → Celery + Redis | |
| Tests | pytest, pytest-cov | |
| Deploy | Docker Compose | |

---

## 11. نطاق Phase 3 — MVP

### 11.1 ما سيُبنى

- [ ] `pipeline/` كامل مع `LocalProvider` (PaddleOCR)
- [ ] المحركات الأربعة + القواعد + القرار
- [ ] DB (SQLite) + التشفير + الإخفاء + التدقيق
- [ ] API: الدفعات، المستندات، المراجعة، التصدير xlsx/csv
- [ ] Streamlit: رفع، تقدّم، لوحة معلومات مع drill-down، شاشة مراجعة (صورة + حقول + تصحيح)
- [ ] ملفات `config/` الستة مبدئية
- [ ] اختبارات الوحدة للمحركات على ExtractionResult ثابتة (بدون صور حقيقية في المستودع)
- [ ] اختبار تكامل على العيّنات الثلاث **محلياً** (الصور خارج المستودع)

### 11.2 يؤجَّل إلى Phase 5

PDF report · Vision-LLM provider · Consensus mode · Next.js · RBAC كامل · Celery · Adapters خارجية (Wathq/Muqeem) · PDF input متعدد الصفحات · كشف "ليست إقامة"

### 11.3 خطة الاختبار (مقابل خطتك في المتطلبات §15)

| الحالة | المصدر | المتوقع |
|---|---|---|
| Valid + Company + Eligible-listed | fixture مصطنعة | `APPROVED` إن `auto_approve=true`؛ وإلا `MANUAL_REVIEW/RECOMMEND_APPROVE` |
| Expired | العيّنة 3 | `REJECTED – Expired` |
| Individual Employer (فقط) | fixture | `REJECTED – Individual Employer` |
| Excluded Occupation (فقط) | fixture | `REJECTED – Excluded Occupation` |
| Multiple failures | العيّنة 1 | `REJECTED` بثلاثة أسباب مرتّبة |
| Unlisted occupation | العيّنة 2 | `MANUAL_REVIEW` |
| Expiring soon | fixture (اليوم + 10) | `MANUAL_REVIEW` مع الأيام المتبقية |
| Nationality blocked | fixture + `mode=BLOCKLIST` | `REJECTED – Nationality not approved` |
| Poor image | العيّنة 1 مصغّرة إلى 300px | `MANUAL_REVIEW – Poor image` |
| Field correction re-decides | العيّنة 2 + تصحيح المهنة لمهنة مؤهلة | القرار يتغيّر ويُحفظ كإصدار 2 |

---

## 12. الأسئلة المعلّقة لهذه المرحلة

| # | السؤال | الافتراض إن لم تُجب |
|---|---|---|
| **Q20** | **هل يوجد قبول آلي؟** (أ) لا — كل ما ليس مرفوضاً قاطعاً يُراجَع · (ب) نعم عند اكتمال الشروط | **(أ)** `auto_approve: false` |
| **Q21** | عتبة «قرب الانتهاء» بالأيام؟ | 30 يوماً |
| **Q22** | قائمة الجنسيات غير المعتمدة (إن وُجدت) — أم نبدأ بـ «الكل معتمد»؟ | `ALL_APPROVED` |
| **Q23** | هل توجد مهن تريد إدراجها **كمؤهلة** من البداية لتقليل المراجعات؟ | لا — تُضاف من الواجهة تدريجياً |

**بموافقتك على هذه الوثيقة أبدأ Phase 3 مباشرةً.** الأسئلة الأربعة لا تحجب البدء — كلها مفاتيح تكوين تُغيَّر لاحقاً بلا كود.
