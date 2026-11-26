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
    assert c.resolve_speaker_to_user_id("Gian Domenico Bianchi") == 9  # complete name
    assert c.resolve_speaker_to_user_id("[Gabriele]") == 8         # unique first name, brackets stripped
    assert c.resolve_speaker_to_user_id("Abbasi") == 7             # unique last name
    assert c.resolve_speaker_to_user_id("Rossi") == 8              # unique last name
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


def test_disambiguate_shared_firstname_via_attendees():
    c = fake_client()
    USERS_DUP = USERS + [
        {"id": 11, "name": "Pietro Amodeo", "login": "pietro.amodeo@a176lab.it", "email": "pietro.amodeo@a176lab.it"},
        {"id": 12, "name": "Pietro Artale", "login": "pietro@a176lab.it", "email": "pietro@a176lab.it"},
    ]
    c._execute = lambda m, meth, a, kw=None: USERS_DUP if (m, meth) == ("res.users", "search_read") else 101
    assert c.resolve_speaker_to_user_id("Pietro") is None
    assert c.resolve_speaker_to_user_id("Pietro", preferred_names=["Pietro Artale"]) == 12


def test_fetch_upcoming_events_and_projects():
    c = OdooMeetingClient(url="http://odoo", db="db", username="u", api_key="k")

    def fake_exec(model, method, args, kwargs=None):
        if (model, method) == ("project.project", "search_read"):
            return [{"id": 81, "name": "A176LAB Gestione"}]
        if (model, method) == ("project.project", "read"):
            return [{"id": 81, "name": "A176LAB Gestione"}]
        if (model, method) == ("calendar.event", "search_read"):
            return [
                {"id": 5, "name": "Mazara Sync", "start": "2026-11-26 09:00:00",
                 "stop": "2026-11-26 10:00:00", "allday": False,
                 "videocall_location": "https://teams.microsoft.com/l/meetup-join/123",
                 "location": False, "description": False,
                 "partner_ids": [99], "res_model": "project.project", "res_id": 81},
                {"id": 6, "name": "Gaia Spinelli in ferie: 1 giorno/i", "start": "2026-11-27 00:00:00",
                 "stop": "2026-11-27 23:59:00", "allday": True,
                 "videocall_location": False, "location": False, "description": False,
                 "partner_ids": [], "res_model": False, "res_id": False},
                {"id": 7, "name": "Reminder without link", "start": "2026-11-26 11:00:00",
                 "stop": "2026-11-26 11:30:00", "allday": False,
                 "videocall_location": False, "location": "Ufficio", "description": "Nota",
                 "partner_ids": [], "res_model": False, "res_id": False},
            ]
        if (model, method) == ("res.users", "search_read"):
            return [{"id": 7, "name": "Muhammad Abbasi", "partner_id": [99, "Muhammad Abbasi"]}]
        raise AssertionError((model, method))

    c._execute = fake_exec
    assert c.fetch_projects() == [{"id": 81, "name": "A176LAB Gestione"}]
    evs = c.fetch_upcoming_events()
    assert len(evs) == 1
    from datetime import datetime, timezone
    expected_dt = datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d_%H%M")
    assert evs[0]["meeting_id"] == f"{expected_dt}_Mazara-Sync"
    assert evs[0]["project_id"] == 81 and evs[0]["project_name"] == "A176LAB Gestione"
    assert evs[0]["attendees"] == ["Muhammad Abbasi"]


def test_publish_creates_knowledge_article_under_meeting_notes():
    c = OdooMeetingClient(url="http://odoo", db="db", username="u", api_key="k", knowledge_parent="Meeting Notes")
    created = []

    def fake_exec(model, method, args, kwargs=None):
        if (model, method) == ("res.users", "search_read"):
            return USERS
        if (model, method) == ("knowledge.article", "search_read"):
            return [{"id": 251}]
        if method == "create":
            created.append((model, args[0]))
            return 500 + len(created)
        raise AssertionError((model, method))

    c._execute = fake_exec
    r = c.publish_meeting_with_subtasks("2026-11-26_1230_Sync", NOISY_MD, ["Muhammad"])
    assert r["knowledge_article_id"] == 506
    k_articles = [vals for model, vals in created if model == "knowledge.article"]
    assert len(k_articles) == 1
    assert k_articles[0]["parent_id"] == 251
    assert "<table" in k_articles[0]["body"] and "Export the VCOM data" in k_articles[0]["body"]
