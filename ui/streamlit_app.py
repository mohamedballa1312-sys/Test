"""Streamlit MVP UI — a pure HTTP client of the API (no business logic here)."""
from __future__ import annotations

import io
import os
import time

import requests
import streamlit as st
from PIL import Image, ImageDraw

API = os.environ.get("IQAMA_API_BASE_URL", "http://127.0.0.1:8000")
API_KEY = os.environ.get("IQAMA_API_KEY")

st.set_page_config(page_title="Iqama Screener", page_icon="🪪", layout="wide")
st.markdown("""<style>
.rtl {direction: rtl; text-align: right; font-family: 'Segoe UI', Tahoma, sans-serif;}
.small {font-size: 0.85em; color: #666;}
.badge {padding: 2px 8px; border-radius: 6px; font-weight: 600;}
.APPROVED {background:#C6EFCE;color:#1e5b2e;} .REJECTED {background:#FFC7CE;color:#7a1c1c;} .MANUAL_REVIEW {background:#FFEB9C;color:#6b4d00;} .ERROR{background:#eee;color:#444;}
</style>""", unsafe_allow_html=True)


def H() -> dict:
    h = {"X-Actor": st.session_state.get("actor", "operator")}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h


def api(method: str, path: str, **kw):
    r = requests.request(method, f"{API}{path}", headers=H(), timeout=600, **kw)
    if r.status_code >= 400:
        try:
            st.error(f"{r.status_code}: {r.json().get('detail')}")
        except Exception:
            st.error(f"{r.status_code}: {r.text[:300]}")
        return None
    return r


def badge(s: str) -> str:
    return f"<span class='badge {s}'>{s.replace('_', ' ')}</span>"


# ---------------- sidebar ----------------
with st.sidebar:
    st.title("🪪 Iqama Screener")
    st.session_state["actor"] = st.text_input("Your name (audit trail)", st.session_state.get("actor", "operator"))
    page = st.radio("Page", ["1 · Upload & Process", "2 · Dashboard", "3 · Manual Review", "4 · Rules", "5 · Audit Log"])
    try:
        h = requests.get(f"{API}/health", timeout=5).json()
        st.caption(f"API ✅ rules `{h['rules_version']}` · OCR `{h['ocr_provider']}`")
    except Exception:
        st.error("API unreachable — start it with `uvicorn app.main:app`")
    st.caption("Card-image screening only. Not an official verification; does not detect forgery.")


# ---------------- 1 upload ----------------
if page.startswith("1"):
    st.header("Upload Iqamas")
    name = st.text_input("Batch name", value=f"batch-{time.strftime('%Y%m%d-%H%M')}")
    files = st.file_uploader("Drag & drop JPG / PNG / PDF (multiple)", type=["jpg", "jpeg", "png", "pdf"], accept_multiple_files=True)
    if st.button("Upload & Process", type="primary", disabled=not files):
        r = api("POST", "/api/v1/batches", json={"name": name})
        if r:
            bid = r.json()["id"]
            up = api("POST", f"/api/v1/batches/{bid}/documents", files=[("files", (f.name, f.getvalue(), f.type)) for f in files])
            if up:
                res = up.json()
                st.success(f"Accepted {len(res['accepted'])} file(s)")
                for rej in res["rejected"]:
                    st.warning(f"Rejected {rej['filename']}: {rej['error']}")
                api("POST", f"/api/v1/batches/{bid}/process")
                st.session_state["batch_id"] = bid
    bid = st.session_state.get("batch_id")
    if bid:
        ph = st.empty()
        bar = st.progress(0)
        while True:
            b = api("GET", f"/api/v1/batches/{bid}")
            if not b:
                break
            b = b.json()
            done = b["processed"]; total = max(1, b["total"])
            ph.markdown(f"**Processing {done} / {b['total']}** — status `{b['status']}`")
            bar.progress(min(1.0, done / total))
            if b["status"] in ("DONE", "FAILED"):
                st.session_state["dash_batch"] = bid
                st.success("Batch complete → open the Dashboard")
                break
            time.sleep(2)


# ---------------- 2 dashboard ----------------
elif page.startswith("2"):
    st.header("Dashboard")
    batches = api("GET", "/api/v1/batches")
    batches = batches.json() if batches else []
    if not batches:
        st.info("No batches yet."); st.stop()
    opts = {f"#{b['id']} {b['name']} ({b['status']}, {b['processed']}/{b['total']})": b["id"] for b in batches}
    default = next((k for k, v in opts.items() if v == st.session_state.get("dash_batch")), list(opts)[0])
    bid = opts[st.selectbox("Batch", list(opts), index=list(opts).index(default))]
    b = api("GET", f"/api/v1/batches/{bid}").json()
    s = b["summary"] or {}
    tiles = [("Total", "total", None), ("Approved", "approved", "APPROVED"), ("Rejected", "rejected", "REJECTED"),
             ("Manual Review", "manual_review", "MANUAL_REVIEW"), ("Expired", "expired", "trigger:Expired"),
             ("Individual Employer", "individual_employer", "trigger:Individual Employer"),
             ("Excluded Occupation", "excluded_occupation", "trigger:Excluded Occupation"), ("Errors", "errors", "ERROR")]
    cols = st.columns(len(tiles))
    for col, (label, key, filt) in zip(cols, tiles):
        if col.button(f"{s.get(key, 0)}\n\n{label}", key=f"tile_{key}", use_container_width=True):
            st.session_state["filter"] = filt
    filt = st.session_state.get("filter")
    params = {}
    if filt and filt.startswith("trigger:"):
        params["trigger"] = filt.split(":", 1)[1]
    elif filt:
        params["decision"] = filt
    q = st.text_input("Search (name, number, employer…)")
    if q:
        params["q"] = q
    st.caption(f"Filter: `{filt or 'all'}`  —  click a tile to drill down")
    docs = api("GET", f"/api/v1/batches/{bid}/documents", params=params).json()
    rows = []
    for d in docs:
        f = d["fields"]; dec = d["decision"] or {}
        chk = {c["check"]: c for c in dec.get("checks", [])}
        rows.append({"ID": d["id"], "File": d["filename"], "Iqama": f.get("iqama_no", {}).get("normalized"),
                     "Name": f.get("name_ar", {}).get("normalized"), "Expiry": f.get("expiry_date", {}).get("normalized"),
                     "Days": chk.get("EXPIRY", {}).get("details", {}).get("days_remaining"),
                     "Employer": f.get("employer_name", {}).get("normalized"), "Emp.Type": chk.get("EMPLOYER", {}).get("label"),
                     "Occupation": f.get("occupation", {}).get("normalized"), "Occ.Status": chk.get("OCCUPATION", {}).get("label"),
                     "Decision": dec.get("status") or d["status"], "Reasons / Triggers": "; ".join(dec.get("reasons") or dec.get("review_triggers") or ([d["error"]] if d["error"] else []))})
    st.dataframe(rows, use_container_width=True, hide_index=True)
    c1, c2, c3 = st.columns(3)
    unmask = c3.checkbox("Unmask ID numbers in export (audited)")
    x = api("GET", f"/api/v1/batches/{bid}/export", params={"format": "xlsx", "unmask": str(unmask).lower()})
    if x: c1.download_button("⬇️ Permit file (Excel)", x.content, f"permit_file_{bid}.xlsx")
    c = api("GET", f"/api/v1/batches/{bid}/export", params={"format": "csv", "unmask": str(unmask).lower()})
    if c: c2.download_button("⬇️ Permit file (CSV)", c.content, f"permit_file_{bid}.csv")


# ---------------- 3 review ----------------
elif page.startswith("3"):
    st.header("Manual Review")
    queue = api("GET", "/api/v1/review/queue").json()
    if not queue:
        st.success("Review queue is empty 🎉"); st.stop()
    labels = {f"#{q['document_id']} · {q['filename']} · {q['recommendation'] or ''}": q["document_id"] for q in queue}
    doc_id = labels[st.selectbox(f"Queue ({len(queue)})", list(labels))]
    d = api("GET", f"/api/v1/documents/{doc_id}", params={"unmask": "true"}).json()
    dec = d["decision"] or {}
    st.markdown(f"**Decision:** {badge(dec.get('status', d['status']))} &nbsp; **Recommendation:** `{dec.get('recommendation')}` &nbsp; quality `{d['quality_score']}`", unsafe_allow_html=True)
    for t in dec.get("review_triggers", []):
        st.warning(t)
    for r in dec.get("reasons", []):
        st.error(r)
    left, right = st.columns([1.1, 1])
    with left:
        img = api("GET", f"/api/v1/documents/{doc_id}/image")
        if img is not None and img.status_code == 200:
            im = Image.open(io.BytesIO(img.content)).convert("RGB")
            focus = st.session_state.get("focus_field")
            draw = ImageDraw.Draw(im)
            for name, fv in d["fields"].items():
                if fv.get("bbox"):
                    x, y, w, h = fv["bbox"]
                    draw.rectangle([x, y, x + w, y + h], outline=(255, 0, 0) if name == focus else (0, 120, 255), width=4 if name == focus else 2)
            st.image(im, use_container_width=True)
            st.caption("Blue: extracted fields · Red: field in focus")
        else:
            st.info("Image no longer available (deleted per retention policy).")
    with right:
        st.subheader("Extracted fields — correct and re-decide")
        order = ["iqama_no", "name_ar", "name_en", "nationality", "expiry_date", "birth_date", "occupation", "employer_id", "employer_name", "issue_place", "work_place"]
        new = {}
        for name in order:
            fv = d["fields"].get(name, {})
            conf = fv.get("confidence", 0.0)
            flag = "🟢" if conf >= 0.75 else ("🟠" if conf >= 0.5 else "🔴")
            src = fv.get("source", "")
            val = st.text_input(f"{flag} {name}  ·  conf {conf:.2f}  ·  {src}{'  ·  ' + fv['note'] if fv.get('note') else ''}",
                                value=fv.get("normalized") or "", key=f"f_{doc_id}_{name}")
            if (val or None) != (fv.get("normalized") or None):
                new[name] = val or None
        c1, c2, c3 = st.columns(3)
        if c1.button("Apply corrections & re-decide", type="primary", disabled=not new):
            r = api("PATCH", f"/api/v1/documents/{doc_id}/fields", json={"fields": new})
            if r:
                st.success(f"New decision: {r.json()['decision']['status']}"); st.rerun()
        note = st.text_input("Reviewer note")
        if c2.button("✅ Approve"):
            if api("POST", f"/api/v1/documents/{doc_id}/review", json={"status": "APPROVED", "note": note}): st.rerun()
        if c3.button("⛔ Reject"):
            if api("POST", f"/api/v1/documents/{doc_id}/review", json={"status": "REJECTED", "note": note}): st.rerun()
        with st.expander("Add this occupation to the reference list"):
            occ = d["fields"].get("occupation", {}).get("normalized") or ""
            oa = st.text_input("Arabic", occ); oe = st.text_input("English", "")
            el = st.selectbox("Eligible?", ["Yes", "No"])
            if st.button("Save occupation"):
                if api("POST", "/api/v1/rules/occupations", json={"occupation_ar": oa, "occupation_en": oe, "eligible": el == "Yes", "reason": "" if el == "Yes" else "Excluded occupation"}):
                    st.success("Saved. Re-apply corrections to re-decide."); st.rerun()
        with st.expander("Checks detail"):
            st.json(dec.get("checks", []))


# ---------------- 4 rules ----------------
elif page.startswith("4"):
    st.header("Rules (no code changes needed)")
    rules = api("GET", "/api/v1/rules").json()
    st.caption(f"Active version `{rules['version']}` · {rules['config'].get('version_note')}")
    tab1, tab2, tab3 = st.tabs(["Occupations", "Nationalities", "Edit files"])
    with tab1:
        st.dataframe(rules["occupations"], use_container_width=True, hide_index=True)
    with tab2:
        st.caption(f"Mode: `{rules['config']['nationality']['mode']}`")
        st.dataframe(rules["nationalities"], use_container_width=True, hide_index=True)
    with tab3:
        fname = st.selectbox("File", rules["files"])
        content = api("GET", f"/api/v1/rules/files/{fname}").json()["content"]
        edited = st.text_area("Content", content, height=400)
        if st.button("Validate & activate"):
            r = api("PUT", f"/api/v1/rules/files/{fname}", json={"content": edited})
            if r:
                st.success(f"Activated version {r.json()['version']}")


# ---------------- 5 audit ----------------
else:
    st.header("Audit Log")
    rows = api("GET", "/api/v1/audit", params={"limit": 500}).json()
    st.dataframe([{**r, "details": str(r["details"])} for r in rows], use_container_width=True, hide_index=True)
