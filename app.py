"""
TrustRAG - the Streamlit web app.
This file only handles the UI; all the real work happens in the src/ modules.
Run with:  streamlit run app.py
"""

import hashlib
import time

import streamlit as st

from src import config, llm, vectorstore
from src.chunker import chunk_pages
from src.generator import generate_answer, is_refusal
from src.injection_scanner import filter_chunks, scan_chunks
from src.llm import LLMError
from src.loader import LoaderError, load_pdf
from src.verifier import verify_answer

st.set_page_config(page_title="TrustRAG", page_icon="🛡️", layout="wide")

# st.session_state survives Streamlit's re-runs (the script re-runs on every click).
if "processed_files" not in st.session_state:
    st.session_state.processed_files = set()   # hashes of files already added
if "last_result" not in st.session_state:
    st.session_state.last_result = None        # the most recent question + answer
if "scan_reports" not in st.session_state:
    st.session_state.scan_reports = {}         # file name -> injection scan report
if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0          # changing this gives a fresh, empty upload box


def file_hash(data: bytes) -> str:
    """A fingerprint of the file's contents, so we can recognise the same file again."""
    return hashlib.sha256(data).hexdigest()


def trust_color(score: float) -> str:
    """Green >= 80%, orange 50-79%, red < 50% (thresholds live in config.py)."""
    if score >= config.TRUST_GREEN:
        return "green"
    if score >= config.TRUST_ORANGE:
        return "orange"
    return "red"


def show_verification(verification: dict | None) -> None:
    """Show the trust score and the claim-by-claim fact-check."""
    st.subheader("🔍 Fact-check")
    if verification is None:
        st.warning("The answer could not be fact-checked this time, so treat it with extra care.")
        return
    if verification["refused"]:
        st.success("Correctly refused – no unsupported claims.")
        return
    score = verification["trust_score"]
    if score is None:
        st.info("No factual claims were found in the answer to check.")
        return

    color = trust_color(score)
    st.markdown(f"### Trust score: :{color}[{score:.0f}%]")
    st.progress(score / 100)

    icons = {"SUPPORTED": "✅", "PARTIALLY_SUPPORTED": "⚠️", "NOT_SUPPORTED": "❌"}
    for claim in verification["claims"]:
        st.markdown(f"{icons[claim['status']]} **{claim['claim']}**")
        if claim["quote"]:
            source = f"{claim['file_name']}, p.{claim['page']}" if claim["file_name"] else "source unknown"
            note = "" if claim["quote_found"] else " *(quote not found word-for-word in the sources)*"
            st.caption(f"“{claim['quote']}” — {source}{note}")
        elif claim["reason"]:
            st.caption(claim["reason"])


def process_upload(uploaded_file, excluded_risks: list[str]) -> None:
    """Load -> chunk -> injection scan -> store one uploaded PDF, with friendly errors."""
    data = uploaded_file.getvalue()
    fingerprint = file_hash(data)
    # Include the exclusion setting, so changing it re-processes the file (scan results are cached = free).
    key = f"{fingerprint}:{','.join(sorted(excluded_risks))}"
    if key in st.session_state.processed_files:
        return  # already done in this session with these settings

    with st.spinner(f"Reading and security-scanning {uploaded_file.name}..."):
        try:
            pages = load_pdf(uploaded_file, uploaded_file.name)
            chunks = chunk_pages(pages)
            progress = st.empty()
            report = scan_chunks(
                chunks, fingerprint,
                on_progress=lambda done, total: progress.caption(f"Security scan: batch {done} of {total} checked"),
            )
            progress.empty()
            safe_chunks = filter_chunks(chunks, report, excluded_risks)
            vectorstore.delete_file(uploaded_file.name)  # remove any older version of this file first
            vectorstore.add_chunks(safe_chunks)
        except (LoaderError, LLMError) as e:
            st.error(f"{uploaded_file.name}: {e}")
            return

    report["excluded_risks"] = list(excluded_risks)
    st.session_state.scan_reports[uploaded_file.name] = report
    st.session_state.processed_files.add(key)
    st.success(f"✅ Successfully read {uploaded_file.name}")
    removed = len(chunks) - len(safe_chunks)
    if removed:
        st.warning(f"🛡️ {removed} suspicious section(s) removed from {uploaded_file.name}. See the Security report.")


def show_security_report() -> None:
    """Show what the injection scanner found in each uploaded file."""
    reports = st.session_state.scan_reports
    if not reports:
        return
    total_flagged = sum(len(r["flagged"]) for r in reports.values())
    title = f"🛡️ Security report: 🔴 {total_flagged} suspicious section(s) found (click to view)" if total_flagged \
        else "🛡️ Security report: no prompt-injection attempts found"
    with st.expander(title):
        icons = {"HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟡"}
        for file_name, report in reports.items():
            st.markdown(f"**{file_name}**: {report['scanned']} sections scanned, {len(report['flagged'])} flagged")
            if not report["llm_checked"]:
                st.caption(
                    f"AI checked {report.get('ai_checked', 0)} of {report['scanned']} sections (a temporary error "
                    "affected the rest; the rule-based scan covered them). To retry, click 'Clear all documents' "
                    "and upload the file again - sections already checked are reused for free."
                )
            for item in report["flagged"]:
                excluded = item["risk"] in report["excluded_risks"]
                status = "removed from the index" if excluded else "kept in the index"
                st.markdown(f"{icons[item['risk']]} **{item['risk']} risk**, page {item['page']} · *{status}*")
                if item["rules"]:
                    st.caption("Matched rules: " + ", ".join(item["rules"]))
                if item["reason"]:
                    st.caption("AI reason: " + item["reason"])
                st.text(item["text"][:300] + ("..." if len(item["text"]) > 300 else ""))
            st.divider()


# ---------------------------------------------------------------------------
# Sidebar: upload documents, see what is loaded, API usage
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("📄 Documents")
    upload_area = st.container()  # filled below, once we know the security setting

    st.subheader("🛡️ Security")
    excluded_risks = st.multiselect(
        "Remove flagged sections with risk level",
        options=["HIGH", "MEDIUM", "LOW"],
        default=config.DEFAULT_EXCLUDED_RISKS,
        help="Uploaded documents are scanned for hidden prompt-injection attacks before they are indexed.",
    )

    with upload_area:
        uploaded_files = st.file_uploader(
            "Upload PDFs", type="pdf", accept_multiple_files=True,
            key=f"uploader_{st.session_state.uploader_key}",
        )
        for uploaded_file in uploaded_files or []:
            process_upload(uploaded_file, excluded_risks)

    st.divider()
    files = vectorstore.list_files()
    if files:
        st.caption("Your documents:")
        for name in files:
            st.write(f"- {name}")
        if st.button("🗑️ Clear all documents"):
            vectorstore.clear()
            st.session_state.processed_files = set()
            st.session_state.last_result = None
            st.session_state.scan_reports = {}
            st.session_state.uploader_key += 1  # also empty the upload box, so files aren't re-added
            st.rerun()
    else:
        st.caption("No documents yet.")

    st.divider()
    st.metric("API calls (since app started)", llm.get_call_count())
    st.caption(f"Provider: {config.PROVIDER} · Answer: {config.ANSWER_MODEL} · Checker: {config.CHECKER_MODEL}")

# ---------------------------------------------------------------------------
# Main area: ask a question, show the answer
# ---------------------------------------------------------------------------
st.title("🛡️ TrustRAG")
st.write("Ask questions about your PDFs. Answers come only from your documents, with page citations.")

key_problem = llm.missing_key_message()
if key_problem:
    st.error(key_problem)
    st.stop()  # nothing else works without a key

show_security_report()

# A form lets the user press Enter to submit.
with st.form("question_form"):
    question = st.text_input("Your question", placeholder="e.g. How many days of annual leave do I get?")
    asked = st.form_submit_button("Ask")

if asked:
    if not question.strip():
        st.warning("Please type a question first.")
    elif vectorstore.count() == 0:
        st.warning("Please upload at least one PDF first.")
    else:
        try:
            started = time.time()
            with st.spinner("Step 1 of 2: searching your documents and writing an answer (usually 5–10 seconds)..."):
                chunks = vectorstore.retrieve(question)
                answer = generate_answer(question, chunks)
            # The fact-check is a bonus: if it fails, still show the answer (with a warning).
            verification = None
            with st.spinner("Step 2 of 2: fact-checking the answer (usually 5–10 seconds)..."):
                try:
                    verification = verify_answer(answer, chunks)
                except LLMError:
                    pass
            st.session_state.last_result = {
                "question": question, "answer": answer, "chunks": chunks, "verification": verification,
                "seconds": round(time.time() - started, 1),
            }
            st.rerun()  # re-run so the sidebar API counter shows the new number
        except LLMError as e:
            st.error(str(e))

result = st.session_state.last_result
if result:
    st.subheader("Answer")
    st.caption(f"Question: {result['question']} · answered and fact-checked in {result.get('seconds', '?')}s")
    if is_refusal(result["answer"]):
        st.info(result["answer"])
    else:
        st.markdown(result["answer"])

    show_verification(result.get("verification"))

    with st.expander(f"📚 Sources used ({len(result['chunks'])} chunks)"):
        for chunk in result["chunks"]:
            st.markdown(f"**{chunk['file_name']}, page {chunk['page']}** · distance {chunk['distance']}")
            st.text(chunk["text"])
            st.divider()
