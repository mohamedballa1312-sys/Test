# النشر على خادم سحابي — دليل خطوة بخطوة

| | |
|---|---|
| **الهدف** | رابط تجريبي يصل إليه فريقك، محمي بكلمة مرور و HTTPS، على خادم تملكونه |
| **المدة** | 15–20 دقيقة (أغلبها بناء الصورة وتنزيل نماذج OCR) |
| **يعمل مع** | أي مزوّد: Oracle Cloud، Hetzner، DigitalOcean، AWS Lightsail، Azure، Google Cloud، STC Cloud… |

> لماذا خادمكم وليس استضافة مجانية؟ النظام يعالج صور هويات (PDPL)، ويحتاج 4 أنوية و 8 GB لـ OCR العربي. الاستضافة المجانية لا توفر هذا ولا يصح وضع بيانات هويات عليها.

## 1. إنشاء الخادم (مرة واحدة)

في لوحة المزوّد أنشئ جهازاً افتراضياً بالمواصفات:

| البند | القيمة |
|---|---|
| نظام التشغيل | **Ubuntu 24.04 LTS** (أو 22.04) |
| المعالج / الذاكرة | **4 vCPU / 8 GB RAM** (الحد الأدنى 2 vCPU / 4 GB للتجربة) |
| القرص | 40 GB |
| المنطقة | الأقرب للسعودية (البحرين، الإمارات، أو أوروبا) |
| المنافذ المفتوحة (Security Group / Firewall) | **22, 80, 443** |
| الدخول | مفتاح SSH |

ستحصل على **عنوان IP عام**.

## 2. (اختياري لكن موصى به) نطاق للـ HTTPS

لو لديكم نطاق (مثل `iqama.company.com`): أضف سجل **A** يشير إلى IP الخادم قبل الخطوة 3. بدونه يعمل النظام على `http://IP` بلا تشفير — مقبول لتجربة قصيرة ببيانات غير حقيقية فقط.

## 3. التثبيت — أمر واحد

ادخل إلى الخادم:
```bash
ssh ubuntu@<IP>
```
ثم شغّل (ضع النطاق إن وُجد، أو اتركه فارغاً):
```bash
curl -fsSL https://raw.githubusercontent.com/mohamedballa1312-sys/Test/claude/github-connection-goihxv/deploy/setup-server.sh | sudo bash -s -- iqama.company.com
```

السكربت يقوم بـ: تثبيت Docker · ضبط الجدار الناري · جلب الكود · **توليد المفاتيح السرية وكلمة مرور الدخول** · بناء وتشغيل الخدمات · طباعة الرابط وبيانات الدخول في النهاية:

```
 URL      : https://iqama.company.com
 Login    : admin  /  Xk3p9QwL2mTz
 API docs : /docs
```

## 4. أول استخدام
1. افتح الرابط وأدخل `admin` وكلمة المرور.
2. صفحة **Rules → Permit template**: ارفع نموذج Word لطلب التصريح.
3. صفحة **Upload**: أدخل الشركة الطالبة وبيانات المشروع، ارفع البطاقات، Process.
4. **Dashboard** → **Manual Review** → **Generate Word/PDF**.

## 5. التشغيل اليومي

```bash
cd /opt/iqama-screener
docker compose -f deploy/docker-compose.prod.yml logs -f          # المتابعة
docker compose -f deploy/docker-compose.prod.yml restart          # إعادة تشغيل
git pull && docker compose -f deploy/docker-compose.prod.yml up -d --build   # تحديث لنسخة أحدث
cat .ui_password                                                  # كلمة مرور الواجهة
```

| ماذا | أين |
|---|---|
| القواعد (المهن، الجنسيات، العتبات) | `/opt/iqama-screener/config/` — تُعدَّل من الواجهة أو مباشرة |
| قاعدة البيانات، الصور المشفّرة، نموذج Word | `/opt/iqama-screener/data/` — **انسخها احتياطياً** |
| المفاتيح السرية | `/opt/iqama-screener/.env` — **لا تُشارك ولا تُفقد** (فقدان `IQAMA_ENC_KEY` = فقدان البيانات المشفّرة) |

**نسخة احتياطية:**
```bash
sudo tar czf iqama-backup-$(date +%F).tgz -C /opt/iqama-screener data config .env
```

## 6. الأمان المطبَّق
- الواجهة والـ API كلاهما خلف **كلمة مرور** (Basic Auth في Caddy) + **مفتاح API** داخلي.
- **HTTPS تلقائي** (Let's Encrypt) عند وجود نطاق، يتجدد وحده.
- المنافذ الداخلية (8000، 8501) غير مكشوفة؛ فقط 80/443.
- البيانات الحساسة والصور **مشفّرة على القرص** (AES-256-GCM)، ولا شيء يغادر الخادم.
- تحويل PDF يعمل داخل الحاوية (LibreOffice مضمَّن).

## 7. استكشاف الأخطاء

| العرض | السبب / الحل |
|---|---|
| الصفحة لا تفتح بعد التثبيت مباشرة | البناء يستغرق 5–10 دقائق أول مرة. `docker compose ... logs -f api` |
| `502` من Caddy | الـ API ما زال يقلع أو ينزّل نماذج OCR. انتظر دقيقتين |
| HTTPS لا يعمل | سجل DNS لم ينتشر بعد، أو المنفذ 80/443 مغلق في لوحة المزوّد |
| بطء المعالجة (> 60 ث/بطاقة) | الخادم أصغر من 4 vCPU؛ أو خفّض `IQAMA_WORKERS` إلى 1 |
| نسيت كلمة المرور | `cat /opt/iqama-screener/.ui_password` |
