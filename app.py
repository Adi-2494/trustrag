"""
TrustRAG - the Streamlit UI. It contains no pipeline logic: every action goes to a backend, which is either
the in-process `TrustRAGService` or, when TRUSTRAG_API_URL is set, the TrustRAG HTTP API.
Run with:  streamlit run app.py
"""

import hashlib
import hmac
import time
from typing import Any

import streamlit as st

from src import config, llm
from src.api_client import backend_from_environment
from src.generator import is_refusal
from src.llm import LLMError
from src.loader import LoaderError
from src.observability import setup_logging
from src.safety import safe_markdown, sanitize_filename
from src.service import UploadRejected

st.set_page_config(page_title="TrustRAG", page_icon="🛡️", layout="wide")
setup_logging(config.LOG_LEVEL)


@st.cache_resource
def get_backend() -> Any:
    return backend_from_environment()


backend = get_backend()


# ---------------------------------------------------------------------------
# Optional password gate (set APP_PASSWORD in .env to turn it on)
# ---------------------------------------------------------------------------
def require_password() -> None:
    """Stop the page until the right password is entered. Wrong guesses trigger a short lockout."""
    if not config.APP_PASSWORD or st.session_state.get("authenticated"):
        return
    st.title("🛡️ TrustRAG")
    locked_until = st.session_state.get("locked_until", 0)
    if time.time() < locked_until:
        st.error(f"Too many wrong passwords. Try again in {int(locked_until - time.time()) + 1} seconds.")
        st.stop()
    with st.form("login"):
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in")
    if submitted:
        # compare_digest takes the same time for any wrong password, so it can't be guessed by timing.
        if hmac.compare_digest(password.encode(), config.APP_PASSWORD.encode()):
            st.session_state.authenticated = True
            st.session_state.failed_logins = 0
            st.rerun()
        st.session_state.failed_logins = st.session_state.get("failed_logins", 0) + 1
        if st.session_state.failed_logins >= config.MAX_LOGIN_ATTEMPTS:
            st.session_state.locked_until = time.time() + config.LOGIN_LOCKOUT_SECONDS
            st.session_state.failed_logins = 0
        st.error("Wrong password.")
    st.stop()


require_password()

# st.session_state survives Streamlit's re-runs (the script re-runs on every click).
if "workspace" not in st.session_state:
    # Each browser session gets its own private document store; old, abandoned ones are cleaned up.
    st.session_state.workspace = backend.new_workspace()
if "processed_files" not in st.session_state:
    st.session_state.processed_files = set()  # hashes of files already added
if "last_result" not in st.session_state:
    st.session_state.last_result = None  # the most recent question + answer
if "scan_reports" not in st.session_state:
    st.session_state.scan_reports = {}  # file name -> injection scan report
if "uploader_key" not in st.session_state:
    st.session_state.uploader_key = 0  # changing this gives a fresh, empty upload box
if "question_times" not in st.session_state:
    st.session_state.question_times = []  # when this session asked questions (for the hourly limit)
if "files_added" not in st.session_state:
    st.session_state.files_added = 0

WORKSPACE = st.session_state.workspace


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


def show_verification(verification: Any) -> None:
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
    st.caption(
        "The trust score shows how well the answer is backed by the documents you uploaded. "
        "It can't tell whether those documents are themselves correct."
    )

    icons = {"SUPPORTED": "✅", "PARTIALLY_SUPPORTED": "⚠️", "NOT_SUPPORTED": "❌"}
    for claim in verification["claims"]:
        # Everything below comes from the model, so it is made safe before rendering (no images or links).
        st.markdown(f"{icons[claim['status']]} **{safe_markdown(claim['claim'])}**")
        if claim["quote"]:
            source = f"{claim['file_name']}, p.{claim['page']}" if claim["file_name"] else "source unknown"
            note = "" if claim["quote_found"] else " *(quote not found word-for-word in the sources)*"
            st.caption(f"“{safe_markdown(claim['quote'])}” — {safe_markdown(str(source))}{note}")
        elif claim["reason"]:
            st.caption(safe_markdown(claim["reason"]))


def process_upload(uploaded_file: Any, excluded_risks: list[str]) -> None:
    """Load -> chunk -> injection scan -> store one uploaded PDF, with friendly errors."""
    data = uploaded_file.getvalue()
    name = sanitize_filename(uploaded_file.name)  # the file name is untrusted too
    fingerprint = file_hash(data)
    # Include the exclusion setting, so changing it re-processes the file (scan results are cached = free).
    key = f"{fingerprint}:{','.join(sorted(excluded_risks))}"
    if key in st.session_state.processed_files:
        return  # already done in this session with these settings

    if st.session_state.files_added >= config.MAX_FILES_PER_SESSION:
        st.error(f"{name}: you can add up to {config.MAX_FILES_PER_SESSION} files per session.")
        return

    with st.spinner(f"Reading and security-scanning {name}..."):
        progress = st.empty()
        try:
            result = backend.upload(
                WORKSPACE,
                name,
                data,
                excluded_risks,
                on_progress=lambda done, total: progress.caption(f"Security scan: batch {done} of {total} checked"),
            )
        except (LoaderError, LLMError, UploadRejected) as e:
            st.error(f"{name}: {e}")
            return
        finally:
            progress.empty()

    report = result["report"]
    st.session_state.scan_reports[name] = report
    st.session_state.processed_files.add(key)
    st.session_state.files_added += 1
    st.success(f"✅ Successfully read {name}")
    removed = result["chunks_removed"]
    if removed:
        st.warning(f"🛡️ {removed} suspicious section(s) removed from {name}. See the Security report.")


def show_security_report() -> None:
    """Show what the injection scanner found in each uploaded file."""
    reports = st.session_state.scan_reports
    if not reports:
        return
    total_flagged = sum(len(r["flagged"]) for r in reports.values())
    title = (
        f"🛡️ Security report: 🔴 {total_flagged} suspicious section(s) found (click to view)"
        if total_flagged
        else "🛡️ Security report: no prompt-injection attempts found"
    )
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
                    st.caption("AI reason: " + safe_markdown(item["reason"]))
                st.text(item["text"][:300] + ("..." if len(item["text"]) > 300 else ""))
            st.divider()


def question_limit_reached() -> bool:
    """True if this session asked MAX_QUESTIONS_PER_HOUR questions in the last hour."""
    now = time.time()
    st.session_state.question_times = [t for t in st.session_state.question_times if now - t < 3600]
    return len(st.session_state.question_times) >= config.MAX_QUESTIONS_PER_HOUR


# ---------------------------------------------------------------------------
# Sidebar: upload documents, see what is loaded, API usage
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("📄 Documents")
    upload_area = st.container()  # filled below, once we know the security setting

    st.subheader("🛡️ Security")
    required = config.DEFAULT_EXCLUDED_RISKS  # set by the administrator; users can't remove these
    st.caption(f"Always removed: {', '.join(required) or 'none'} risk sections (set by the administrator).")
    extra = st.multiselect(
        "Also remove",
        options=[r for r in ["HIGH", "MEDIUM", "LOW"] if r not in required],
        help="Uploaded documents are scanned for hidden prompt-injection attacks before they are indexed. "
        "You can make this stricter, but not weaker.",
    )
    excluded_risks = sorted(set(required) | set(extra), key=["HIGH", "MEDIUM", "LOW"].index)

    with upload_area:
        uploaded_files = st.file_uploader(
            "Upload PDFs",
            type="pdf",
            accept_multiple_files=True,
            key=f"uploader_{st.session_state.uploader_key}",
        )
        for uploaded_file in uploaded_files or []:
            process_upload(uploaded_file, excluded_risks)

    st.divider()
    files = backend.list_files(WORKSPACE)
    if files:
        st.caption("Your documents (private to this session):")
        for name in files:
            st.text(f"• {name}")
        if st.button("🗑️ Clear all documents"):
            backend.clear(WORKSPACE)  # only this session's documents
            st.session_state.processed_files = set()
            st.session_state.last_result = None
            st.session_state.scan_reports = {}
            st.session_state.files_added = 0
            st.session_state.uploader_key += 1  # also empty the upload box, so files aren't re-added
            st.rerun()
    else:
        st.caption("No documents yet.")

    st.divider()
    st.metric("API calls (since app started)", llm.get_call_count())

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
    question = st.text_input("Your question", placeholder="e.g. How many days of annual leave do I get?", max_chars=500)
    asked = st.form_submit_button("Ask")

if asked:
    if not question.strip():
        st.warning("Please type a question first.")
    elif backend.count(WORKSPACE) == 0:
        st.warning("Please upload at least one PDF first.")
    elif question_limit_reached():
        st.warning(f"You've asked {config.MAX_QUESTIONS_PER_HOUR} questions in the last hour. Please wait a while.")
    else:
        st.session_state.question_times.append(time.time())
        try:
            with st.spinner(
                "Searching your documents, writing an answer and fact-checking it (usually 5–15 seconds)..."
            ):
                st.session_state.last_result = backend.ask(WORKSPACE, question)
            st.rerun()  # re-run so the sidebar API counter shows the new number
        except LLMError as e:
            st.error(str(e))

result = st.session_state.last_result
if result:
    st.subheader("Answer")
    st.caption(
        f"Question: {safe_markdown(result['question'])} · answered and fact-checked in {result.get('seconds', '?')}s"
    )
    if is_refusal(result["answer"]):
        st.info(result["answer"])
    else:
        st.markdown(safe_markdown(result["answer"]))  # no images/links from the model

    show_verification(result.get("verification"))

    with st.expander(f"📚 Sources used ({len(result['chunks'])} chunks)"):
        for chunk in result["chunks"]:
            match = f"distance {chunk['distance']}" if chunk.get("distance") is not None else "keyword match"
            st.markdown(f"**{chunk['file_name']}, page {chunk['page']}** · {match}")
            st.text(chunk["text"])
            st.divider()
