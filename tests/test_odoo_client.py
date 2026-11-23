import pytest

from server.odoo_client import ActionItem, OdooMeetingClient

USERS = [
    {"id": 7, "name": "Muhammad Abbasi", "login": "muhammad.abbasi.team@gmail.com", "email": "muhammad.abbasi.team@gmail.com"},
    {"id": 8, "name": "Gabriele Rossi", "login": "gabriele", "email": "gabriele@a176lab.it"},
    {"id": 9, "name": "Gian Domenico Bianchi", "login": "gd", "email": "gd@a176lab.it"},
    {"id": 10, "name": "Gabriella Verdi", "login": "gverdi", "email": "gverdi@a176lab.it"},
]

NOISY_MD = """## Executive Summary
Short.

## Key Decisions
- **Grid event** confirmed, not hardware.

## Action Items

| **Owner** | Task | Deadline |
|:---|:--- | :---: |
| **Gabriele** | Export the VCOM data | 27/11/2026 |
| Ginadomenico |  Distribuire la suite v3 su tutti i PC  | 2026-12-01 |
| Muhammad | `Add SCADA log check` |  |
| Remote-Call | Send updated contract | next week |
|  | - | |
"""


def fake_client(**kw):
    c = OdooMeetingClient(url="http://odoo", db="db", username="u", api_key="k", project_id=3, **kw)
    calls = []

    def _execute(model, method, args, kwargs=None):
        calls.append((model, method, args, kwargs or {}))
        if (model, method) == ("res.users", "search_read"):
            return USERS
        if (model, method) == ("project.task", "create"):
            return 100 + len([c for c in calls if c[1] == "create"])
        raise AssertionError((model, method))

    c._execute = _execute
    c.calls = calls
    return c


def test_parse_noisy_action_table():
    items = OdooMeetingClient.parse_action_items_from_markdown(NOISY_MD)
    assert items == [
        ActionItem("Gabriele", "Export the VCOM data", "27/11/2026"),
        ActionItem("Ginadomenico", "Distribuire la suite v3 su tutti i PC", "2026-12-01"),
        ActionItem("Muhammad", "Add SCADA log check", None),
        ActionItem("Remote-Call", "Send updated contract", "next week"),
    ]
    assert OdooMeetingClient.parse_action_items_from_markdown("## Executive Summary\nno table") == []


def test_normalize_deadline_formats():
    n = OdooMeetingClient._normalize_deadline
    assert n("27/11/2026") == "2026-11-27" and n("2026-11-27") == "2026-11-27"
    assert n("**01-12-2026**") == "2026-12-01" and n("01.12.2026") == "2026-12-01"
    assert n("next week") is None and n(None) is None and n("") is None


def test_markdown_to_html_lists_and_bold():
    html = OdooMeetingClient._markdown_to_basic_html(NOISY_MD)
    assert "<h3>Executive Summary</h3>" in html and "<p>Short.</p>" in html
    assert "<ul>\n<li><strong>Grid event</strong> confirmed, not hardware.</li>\n</ul>" in html
    assert "|" not in html  # tables are not dumped into the description


def test_resolve_alias_exact_firstname_fuzzy_unresolved():
    c = fake_client(speaker_aliases={"Ginadomenico": "Gian Domenico Bianchi"})
    assert c.resolve_speaker_to_user_id("Ginadomenico") == 9      # alias -> exact
    assert c.resolve_speaker_to_user_id("[Gabriele]") == 8         # unique first name, brackets stripped
    assert c.resolve_speaker_to_user_id("muhammad.abbasi.team@gmail.com") == 7
    assert c.resolve_speaker_to_user_id("Muhamad Abasi") == 7      # STT misspelling -> fuzzy
    assert c.resolve_speaker_to_user_id("Remote-Call") is None
    assert c.resolve_speaker_to_user_id("Unassigned") is None
    assert c.resolve_speaker_to_user_id("Zorro") is None


def test_publish_creates_parent_and_assigned_subtasks():
    c = fake_client(speaker_aliases={"Ginadomenico": "Gian Domenico Bianchi"})
    r = c.publish_meeting_with_subtasks("daily", NOISY_MD, ["Muhammad", "Gabriele", "Ginadomenico", "Remote-Call"])
    creates = [args[0] for model, method, args, _ in c.calls if method == "create"]
    parent, subs = creates[0], creates[1:]
    assert parent["project_id"] == 3 and parent["user_ids"] == [(6, 0, [7, 8, 9])]
    assert parent["name"].startswith("[Meeting Notes] daily (") and "<h3>Executive Summary</h3>" in parent["description"]
    assert all(s["parent_id"] == r["parent_task_id"] == 101 for s in subs)
    assert subs[0]["user_ids"] == [(6, 0, [8])] and subs[0]["date_deadline"] == "2026-11-27"
    assert subs[1]["user_ids"] == [(6, 0, [9])] and subs[1]["date_deadline"] == "2026-12-01"
    assert subs[2]["user_ids"] == [(6, 0, [7])] and "date_deadline" not in subs[2]
    assert subs[3]["name"] == "[Remote-Call] Send updated contract" and "user_ids" not in subs[3]
    assert r["status"] == "published" and len(r["subtasks"]) == 4
    assert r["speaker_user_map"] == {"Muhammad": 7, "Gabriele": 8, "Ginadomenico": 9, "Remote-Call": None}


def test_publish_or_save_when_unconfigured(tmp_path, monkeypatch):
    for k in ("ODOO_URL", "ODOO_DB", "ODOO_USER", "ODOO_API_KEY", "ODOO_PROJECT_ID", "SPEAKER_ALIASES"):
        monkeypatch.delenv(k, raising=False)
    r = OdooMeetingClient().publish_or_save("daily", "# x\n", ["A"], tmp_path)
    assert r["status"] == "saved_locally" and (tmp_path / "daily.minutes.md").read_text() == "# x\n"


def test_publish_or_save_when_odoo_down(tmp_path):
    c = OdooMeetingClient(url="http://127.0.0.1:1", db="db", username="u", api_key="k")
    r = c.publish_or_save("daily", "# x\n", ["A"], tmp_path)
    assert r["status"] == "saved_locally" and "reason" in r and (tmp_path / "daily.minutes.md").exists()
