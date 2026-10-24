"""
TrustRAG - the Streamlit web app.
This file only handles the UI; all the real work happens in the src/ modules.
Run with:  streamlit run app.py
"""

import hashlib

import streamlit as st

from src import config, llm, vectorstore
from src.chunker import chunk_pages
from src.generator import generate_answer, is_refusal
from src.llm import LLMError
from src.loader import LoaderError, load_pdf

st.set_page_config(page_title="TrustRAG", page_icon="🛡️", layout="wide")

# st.session_state survives Streamlit's re-runs (the script re-runs on every click).
if "processed_files" not in st.session_state:
    st.session_state.processed_files = set()   # hashes of files already added
if "last_result" not in st.session_state:
    st.session_state.last_result = None        # the most recent question + answer


def file_hash(data: bytes) -> str:
    """A fingerprint of the file's contents, so we can recognise the same file again."""
    return hashlib.sha256(data).hexdigest()


def process_upload(uploaded_file) -> None:
    """Load -> chunk -> store one uploaded PDF, showing progress and friendly errors."""
    data = uploaded_file.getvalue()
    fingerprint = file_hash(data)
    if fingerprint in st.session_state.processed_files:
        return  # already done in this session

    with st.spinner(f"Processing {uploaded_file.name}... (scanned pages are read with OCR and take longer)"):
        try:
            pages = load_pdf(uploaded_file, uploaded_file.name)
            chunks = chunk_pages(pages)
            vectorstore.add_chunks(chunks)
        except (LoaderError, LLMError) as e:
            st.sidebar.error(f"{uploaded_file.name}: {e}")
            return
    st.session_state.processed_files.add(fingerprint)
    st.sidebar.success(f"✅ Successfully read {uploaded_file.name}")


# ---------------------------------------------------------------------------
# Sidebar: upload documents, see what is loaded, API usage
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("📄 Documents")
    uploaded_files = st.file_uploader("Upload PDFs", type="pdf", accept_multiple_files=True)
    for uploaded_file in uploaded_files or []:
        process_upload(uploaded_file)

    files = vectorstore.list_files()
    if files:
        st.caption("Your documents:")
        for name in files:
            st.write(f"- {name}")
        if st.button("🗑️ Clear all documents"):
            vectorstore.clear()
            st.session_state.processed_files = set()
            st.session_state.last_result = None
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
            with st.spinner("Searching your documents and writing an answer..."):
                chunks = vectorstore.retrieve(question)
                answer = generate_answer(question, chunks)
            st.session_state.last_result = {"question": question, "answer": answer, "chunks": chunks}
            st.rerun()  # re-run so the sidebar API counter shows the new number
        except LLMError as e:
            st.error(str(e))

result = st.session_state.last_result
if result:
    st.subheader("Answer")
    st.caption(f"Question: {result['question']}")
    if is_refusal(result["answer"]):
        st.info(result["answer"])
    else:
        st.markdown(result["answer"])

    with st.expander(f"📚 Sources used ({len(result['chunks'])} chunks)"):
        for chunk in result["chunks"]:
            st.markdown(f"**{chunk['file_name']}, page {chunk['page']}** · distance {chunk['distance']}")
            st.text(chunk["text"])
            st.divider()
