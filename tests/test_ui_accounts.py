"""
P6 Streamlit UI (plan 2.4 `app.py` row, threat rows 39 and 42, SPEC-GAP-035, -047).

Streamlit 1.66.1 ships `streamlit.testing.v1.AppTest`, which runs `app.py` in-process. The backend is replaced through
`src.api_client.backend_from_environment` (what `app.py` calls), so the UI talks to a `TrustRAGService` built on fakes.
`st.context.ip_address` is a MagicMock under AppTest, so the tests pin it to None (localhost) or to a fixed address.

UI contract fixed by these tests (SPEC-GAP-057); widgets are found by `key`, not by label:
    accounts disabled   v1.0 behaviour: the anonymous workspace is created on the first page load, no new widgets.
    accounts enabled    after the APP_PASSWORD gate, a choice page: text_input `signin_username`, text_input
                        `signin_password` (password), form submit button `signin_submit`, button `continue_anonymous`.
                        The anonymous workspace is created only after "continue_anonymous" (SPEC-GAP-047).
    signed in           session_state: `session` (token), `username`, `workspace`; sidebar: button `sign_out`,
                        selectbox `workspace_select` (workspace ids), button `new_workspace`.
                        A signed-in account without workspaces gets one created automatically.
    backend calls       `backend.login(username, password, client_key=<ip or None>)`, `backend.logout(session)`,
                        `backend.list_workspaces(session=...)`, and every existing method with `session=...`.
    state clearing      on sign-in, sign-out and session expiry every session_state key is removed except
                        `authenticated`, `failed_logins`, `locked_until` (the APP_PASSWORD keys).
    messages            "Wrong username or password." / "Too many sign-in attempts. Please try again later."
"""

from pathlib import Path

import pytest

from src import config
from tests.fakes import FakeLLM, FakeStore
from tests.p6_support import HANDBOOK, PASSWORD, add_account, make_service, sign_in

APP = str(Path(__file__).resolve().parent.parent / "app.py")
APP_KEYS = {"authenticated", "failed_logins", "locked_until"}
SECRET_ANSWER = "SECRET-ANSWER-TEXT-ALICE"
RESULT_KEYS = ("last_result", "scan_reports", "processed_files", "files_added")


def _launch(monkeypatch, service, *, accounts, ip=None):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    from src import api_client, llm

    # The backend uses a fake LLM, so the page must not depend on this machine having the Claude CLI or an API key
    # (CI has neither; without this the page stops at "Claude Code is not installed").
    monkeypatch.setattr(llm, "missing_key_message", lambda: None)
    monkeypatch.setattr(config, "ACCOUNTS_ENABLED", accounts, raising=False)
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    monkeypatch.setattr(api_client, "backend_from_environment", lambda: service)
    monkeypatch.setattr(type(st.context), "ip_address", property(lambda self: ip))
    st.cache_resource.clear()
    return AppTest.from_file(APP, default_timeout=60)


def _page_text(at) -> str:
    elements = [*at.markdown, *at.caption, *at.text, *at.info, *at.subheader, *at.header, *at.title, *at.warning]
    return "\n".join(str(getattr(el, "value", "")) for el in elements)


def _fill_sign_in(at, username, password=PASSWORD):
    at.text_input(key="signin_username").input(username)
    at.text_input(key="signin_password").input(password)
    at.button(key="signin_submit").click()
    return at.run()


def _absent_or_empty(at, key) -> bool:
    """Cleared state may be a missing key or an initial value (None, empty, 0)."""
    return key not in at.session_state or not at.session_state[key]


def _anonymous_workspaces(store):
    return [w for w in store.workspaces if not w.startswith("acct-")]


@pytest.fixture
def ui(monkeypatch, fast_kdf):
    """store, service, a launcher for AppTest and alice's account; the flag is passed to the launcher."""

    class Ui:
        store = FakeStore()
        service = make_service(store=store, llm=FakeLLM(answer=f"{SECRET_ANSWER} [handbook.pdf, p.1]."))

        def launch(self, *, accounts=True, ip=None):
            return _launch(monkeypatch, self.service, accounts=accounts, ip=ip)

    return Ui()


def test_ui_first_load_unchanged_when_accounts_disabled(ui):
    """P6-AC3 (baseline: passes on v1.0): flag off -> workspace created on the first load, no sign-in widgets."""
    at = ui.launch(accounts=False).run()
    assert not at.exception
    assert len(ui.store.workspaces) == 1
    workspace = at.session_state["workspace"]
    assert workspace in ui.store.workspaces and not workspace.startswith("acct-")
    assert at.get("text_input") and [b.key for b in at.button] == ["FormSubmitter:question_form-Ask"]
    keys = {el.key for el in [*at.text_input, *at.button, *at.selectbox]}
    assert not keys & {
        "signin_username",
        "signin_password",
        "signin_submit",
        "continue_anonymous",
        "sign_out",
        "workspace_select",
        "new_workspace",
    }
    assert "session" not in at.session_state


def test_ui_accounts_enabled_shows_choice_and_creates_no_workspace_before_it(ui):
    """P6 / SPEC-GAP-047: flag on -> sign-in or continue anonymously first; no workspace exists before the choice."""
    at = ui.launch().run()
    assert not at.exception
    assert (
        at.text_input(key="signin_username") and at.text_input(key="signin_password") and at.button(key="signin_submit")
    )
    assert at.button(key="continue_anonymous")
    assert ui.store.workspaces == {} and "workspace" not in at.session_state
    at.button(key="continue_anonymous").click().run()  # anonymous mode, B5 as before
    assert not at.exception and len(_anonymous_workspaces(ui.store)) == 1 and "session" not in at.session_state


def test_ui_sign_in_opens_the_first_account_workspace(ui):
    """P6 / SPEC-GAP-035: signing in keeps the token only in session_state and lands in an owned workspace."""
    add_account("alice")
    at = ui.launch().run()
    _fill_sign_in(at, "alice")
    assert not at.exception
    assert at.session_state["session"] and at.session_state["username"] == "alice"
    workspace = at.session_state["workspace"]
    assert workspace.startswith("acct-") and list(ui.store.workspaces) == [workspace]
    assert _anonymous_workspaces(ui.store) == []
    assert at.button(key="sign_out") and at.selectbox(key="workspace_select")


def test_ui_sign_out_clears_answer_sources_and_workspace(ui):
    """P6-AC4 / threat 39 (review #9): after sign-out nothing of the previous user's session or answer is left."""
    add_account("alice")
    at = ui.launch().run()
    _fill_sign_in(at, "alice")
    token, workspace = at.session_state["session"], at.session_state["workspace"]
    ui.service.upload(workspace, "handbook.pdf", HANDBOOK, session=token)
    answer = ui.service.ask(workspace, "annual leave?", session=token)
    at.session_state["last_result"] = answer
    at.session_state["scan_reports"] = {
        "handbook.pdf": {"scanned": 3, "ai_checked": 3, "flagged": [], "llm_checked": True, "excluded_risks": ["HIGH"]}
    }
    at.session_state["processed_files"] = {"abc"}
    at.session_state["files_added"] = 2
    at.session_state["authenticated"] = True  # the APP_PASSWORD keys must survive
    at.session_state["failed_logins"] = 2
    at.session_state["locked_until"] = 0
    at.run()
    assert SECRET_ANSWER in _page_text(at)  # sanity: the answer was on screen while signed in

    at.button(key="sign_out").click().run()
    assert not at.exception
    for key in ("session", "username", "workspace"):
        assert key not in at.session_state, key
    for key in RESULT_KEYS:
        assert _absent_or_empty(at, key), key
    assert (at.session_state["authenticated"], at.session_state["failed_logins"], at.session_state["locked_until"]) == (
        True,
        2,
        0,
    )
    assert SECRET_ANSWER not in _page_text(at) and "handbook.pdf" not in _page_text(at)
    assert at.text_input(key="signin_username") and at.button(key="continue_anonymous")  # back at the choice page
    assert _anonymous_workspaces(ui.store) == []  # signing out never creates an anonymous workspace
    from src.service import NotAuthenticated

    with pytest.raises(NotAuthenticated):
        ui.service.list_workspaces(session=token)


def test_ui_expired_session_does_not_fall_back_to_anonymous(ui):
    """P6-AC4 / threat 39 (review #9): a session that ends server-side shows the sign-in page, never anonymous mode."""
    add_account("alice")
    at = ui.launch().run()
    _fill_sign_in(at, "alice")
    token = at.session_state["session"]
    at.session_state["last_result"] = {"leftover": True}
    ui.service.logout(token)  # the session expires / is revoked on the server (idle, absolute, disable, reset)
    at.run()
    assert not at.exception
    for key in ("session", "username", "workspace"):
        assert key not in at.session_state, key
    assert _absent_or_empty(at, "last_result")
    assert at.text_input(key="signin_username") and at.button(key="signin_submit")
    assert _anonymous_workspaces(ui.store) == []


def test_ui_workspace_switch_resets_results(ui):
    """P6-AC4 / threat 39: choosing another workspace drops the previous answer, reports, file hashes and uploader."""
    add_account("alice")
    token = sign_in(ui.service, "alice")
    first, second = ui.service.new_workspace(session=token), ui.service.new_workspace(session=token)
    at = ui.launch().run()
    _fill_sign_in(at, "alice")
    assert at.session_state["workspace"] in (first, second)
    current = at.session_state["workspace"]
    other = second if current == first else first
    uploader_before = at.session_state["uploader_key"]
    at.session_state["last_result"] = {
        "question": "q",
        "answer": SECRET_ANSWER,
        "refused": False,
        "chunks": [],
        "verification": None,
        "seconds": 0.1,
        "request_id": "r",
    }
    at.session_state["scan_reports"] = {"old.pdf": {}}
    at.session_state["processed_files"] = {"hash-1"}
    at.session_state["files_added"] = 4
    at.run()
    at.selectbox(key="workspace_select").select(other).run()
    assert not at.exception and at.session_state["workspace"] == other
    assert at.session_state["last_result"] is None and at.session_state["scan_reports"] == {}
    assert not at.session_state["processed_files"] and at.session_state["files_added"] == 0
    assert at.session_state["uploader_key"] != uploader_before
    assert SECRET_ANSWER not in _page_text(at)


def test_ui_without_client_address_does_not_share_one_lockout_bucket(ui, monkeypatch):
    """P6-AC4 / threat 42 (review #3): ip_address None -> client_key None (no shared 'unknown' bucket)."""
    add_account("alice")
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES_PER_CLIENT", 3, raising=False)
    seen, real = [], ui.service.login

    def spy(*args, **kwargs):
        seen.append(kwargs.get("client_key"))
        return real(*args, **kwargs)

    monkeypatch.setattr(ui.service, "login", spy)
    at = ui.launch(ip=None).run()
    for name in ("user-a", "user-b", "user-c", "user-d", "user-e"):  # more failures than the per-client limit
        _fill_sign_in(at, name, "wrong password 1234")
        shown = " ".join(e.value for e in at.error)
        assert "Wrong username or password." in shown and "Too many" not in shown
    _fill_sign_in(at, "alice")
    assert at.session_state["session"] and not at.exception  # another person is not locked out by them
    assert seen == [None] * 6


def test_ui_passes_a_known_client_address_as_client_key(ui, monkeypatch):
    """P6 / SPEC-GAP-036: when Streamlit knows the address it is used as the per-client key."""
    seen, real = [], ui.service.login
    monkeypatch.setattr(ui.service, "login", lambda *a, **k: seen.append(k.get("client_key")) or real(*a, **k))
    at = ui.launch(ip="203.0.113.9").run()
    _fill_sign_in(at, "nobody", "wrong password 1234")
    assert seen == ["203.0.113.9"]


def test_ui_clear_documents_keeps_account_workspace(ui):
    """P6-AC4 / SPEC-GAP-058 (review M2): 'Clear all documents' empties the workspace, it does not delete it."""
    add_account("alice")
    at = ui.launch().run()
    _fill_sign_in(at, "alice")
    token, workspace = at.session_state["session"], at.session_state["workspace"]
    ui.service.upload(workspace, "handbook.pdf", HANDBOOK, session=token)
    at.run()
    clear = [b for b in at.button if "Clear all documents" in str(b.label)]
    assert len(clear) == 1, "the clear button should be shown while the workspace has documents"
    clear[0].click().run()

    assert not at.exception
    assert ui.store.count(workspace) == 0  # documents gone
    assert [i["workspace"] for i in ui.service.list_workspaces(session=token)["workspaces"]] == [workspace]
    assert at.session_state["workspace"] == workspace  # still selected
    assert at.selectbox(key="workspace_select").value == workspace
