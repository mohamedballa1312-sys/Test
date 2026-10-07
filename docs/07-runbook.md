# دليل التشغيل (Runbook) — Iqama Screener

| | |
|---|---|
| **النطاق** | خادم الطيار/الإنتاج المثبّت بـ `deploy/setup-server.sh` في `/opt/iqama-screener` |
| **المالك** | مالك النظام (انظر `docs/08-raci.md`) |
| **آخر تحديث** | 2026-10-07 |

كل الأوامر تُنفَّذ على الخادم بصلاحية `sudo` من داخل `/opt/iqama-screener` ما لم يُذكر غير ذلك. `DC` اختصار لـ `docker compose -f deploy/docker-compose.prod.yml --env-file .env`.

## 1. الحالة والسجلات
```bash
DC ps                       # حالة الخدمات (api, ui, caddy)
curl -s http://127.0.0.1/health          # {"status":"ok", ...}
DC logs -f --tail=200 api   # سجل الـ API (JSON، أرقام الهوية مُخفاة)
DC logs -f caddy            # الوصول و TLS
```

## 2. إعادة التشغيل
```bash
DC restart api              # خدمة واحدة
DC down && DC up -d         # الكل — المستندات العالقة في PROCESSING تُعاد للطابور تلقائياً عند الإقلاع
```

## 3. التحديث إلى نسخة أحدث
```bash
git fetch && git log --oneline HEAD..origin/main   # ما الجديد
git pull && DC up -d --build                      # توقف قصير أثناء البناء
curl -s http://127.0.0.1/health
```
**الرجوع للنسخة السابقة:** `git log --oneline -5` ثم `git checkout <commit> && DC up -d --build`. تغييرات قاعدة البيانات إضافية فقط (أعمدة جديدة) فالرجوع آمن.

## 4. النسخ الاحتياطي والاستعادة
```bash
deploy/backup.sh                         # يدوياً (الليلي عبر cron الساعة 02:00 UTC)
ls -la backups/                          # الأرشيفات المشفّرة + sha256
deploy/restore.sh backups/iqama-2026-10-07-0200.tar.gz.age   # يوقف، يستعيد، يشغّل، يفحص الصحة
```
الاستعادة تحتاج مفتاح فك التشفير (`BACKUP_AGE_IDENTITY`) **المحفوظ خارج الخادم**. **تمرين الاستعادة:** مرة شهرياً على خادم تجريبي، وتسجيل المدة في جدول القسم 9.

## 5. كلمات المرور والمفاتيح
| ماذا | كيف |
|---|---|
| كلمة مرور الواجهة (admin) | `cat .ui_password` |
| تغييرها | `NEW=$(openssl rand -base64 12 \| tr -d '/+=' \| cut -c1-14); H=$(docker run --rm caddy:2 caddy hash-password --plaintext "$NEW"); sed -i "s#^UI_PASSWORD_HASH=.*#UI_PASSWORD_HASH=$H#" .env; echo "$NEW" > .ui_password; DC up -d caddy` |
| مفتاح API | في `.env` (`IQAMA_API_KEY`)؛ تغييره = تعديل القيمة ثم `DC up -d api ui` |
| مفتاح التشفير `IQAMA_ENC_KEY` | **لا يُغيَّر يدوياً** — فقدانه = فقدان كل البيانات المشفّرة. نسخة الطوارئ (escrow) في مدير أسرار المزوّد ونسخة مختومة لدى مسؤول ثانٍ (انظر `docs/06` §6) |

## 6. القواعد (المهن، الجنسيات، العتبات)
من صفحة Rules في الواجهة، أو بتحرير `config/*.csv|yaml` ثم `curl -X POST -H "X-API-Key: $KEY" http://127.0.0.1/api/v1/rules/reload`. الملف المعطوب يُرفض وتبقى النسخة السابقة فعّالة. كل تغيير مُسجَّل في سجل التدقيق بإصدار (hash).

## 7. الاحتفاظ والحذف
- صور البطاقات تُحذف تلقائياً بعد إصدار طلب التصريح.
- التطهير الدوري (`data_retention_days`) يعمل داخل الـ API كل `IQAMA_PURGE_INTERVAL_HOURS` ساعة ويُسجَّل في التدقيق.
- حذف فوري لطلب صاحب بيانات: `curl -X DELETE -H "X-API-Key: $KEY" http://127.0.0.1/api/v1/documents/<id>` (يحذف البيانات والصورة ويسجّل الحدث).

## 8. استكشاف الأخطاء
| العرض | التشخيص | الإجراء |
|---|---|---|
| الـ API لا يقلع: `InsecureStartupError` | `IQAMA_API_KEY` ناقص/قصير أو `IQAMA_PUBLIC_URL` ليس https | أصلح `.env` ثم `DC up -d api` |
| `502` من Caddy | الـ API يقلع أو ينزّل نماذج OCR | انتظر دقيقتين؛ `DC logs api` |
| HTTPS لا يعمل | DNS لم ينتشر أو المنفذ 80/443 مغلق | تحقق من سجل A والجدار الناري لدى المزوّد |
| `429 Too Many Requests` | حد المعدل (120 طلب/دقيقة، 10 رفعات/دقيقة لكل IP) | انتظر دقيقة؛ ارفع الحد في `deploy/Caddyfile` إن لزم |
| `413` عند الرفع | أكثر من 200 ملف أو حجم > 300 MB | قسّم الدفعة |
| المعالجة بطيئة (> 60 ث/بطاقة) | خادم أصغر من 4 vCPU | `IQAMA_WORKERS=1`؛ ترقية الخادم |
| القرص ممتلئ | نسخ احتياطية قديمة | `find backups -mtime +30 -delete`؛ تحقق من `BACKUP_KEEP_DAYS` |
| ملف التصريح لا يفتح | ZIP مشفّر AES | يُفتح بـ 7-Zip / WinRAR / Keka بكلمة المرور المرسلة عبر قناة أخرى |

## 9. سجل تمارين الاستعادة
| التاريخ | النسخة | المدة | النتيجة | المنفّذ |
|---|---|---|---|---|
| 2026-10-07 | تجريبية (بيئة البناء، gpg، بلا Docker) | < 5 ثوانٍ للبيانات؛ إقلاع الحاوية يُضاف على الخادم | ✅ قاعدة البيانات والصور والقواعد استُعيدت كاملة | Claude (آلي) |

## 10. جهات الاتصال
انظر `docs/08-raci.md`.
