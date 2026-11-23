"""Odoo 18 XML-RPC client: parent `project.task` with the minutes + one assigned sub-task per
action item. Ported from odoo_integration_idea.txt; adds `publish_or_save` with a local fallback."""
from __future__ import annotations

import json
import logging
import os
import re
import xmlrpc.client
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

UNASSIGNED = {"unassigned", "tbd", "unknown", "all", "team", "remote-call", "", "-"}


@dataclass
class ActionItem:
    owner: str
    task: str
    deadline: str | None = None


class OdooMeetingClient:
    def __init__(self, url: str | None = None, db: str | None = None, username: str | None = None,
                 api_key: str | None = None, project_id: int | None = None,
                 speaker_aliases: dict[str, str] | None = None) -> None:
        self.url = (url or os.getenv("ODOO_URL", "")).rstrip("/")
        self.db = db or os.getenv("ODOO_DB", "")
        self.username = username or os.getenv("ODOO_USER", "")
        self.api_key = api_key or os.getenv("ODOO_API_KEY", "")
        raw_project_id = project_id or os.getenv("ODOO_PROJECT_ID")
        self.project_id = int(raw_project_id) if raw_project_id else None
        if speaker_aliases is None and os.getenv("SPEAKER_ALIASES"):
            speaker_aliases = json.loads(os.environ["SPEAKER_ALIASES"])
        self.speaker_aliases = {k.strip().lower(): v.strip() for k, v in (speaker_aliases or {}).items()}
        self._uid: int | None = None
        self._models: xmlrpc.client.ServerProxy | None = None
        self._users_cache: list[dict[str, Any]] | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.url and self.db and self.username and self.api_key)

    def _connect(self) -> tuple[int, xmlrpc.client.ServerProxy]:
        if not self.is_configured:
            raise RuntimeError("Odoo XML-RPC credentials are incomplete (ODOO_URL/DB/USER/API_KEY).")
        if self._uid is None or self._models is None:
            common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", allow_none=True)
            uid = common.authenticate(self.db, self.username, self.api_key, {})
            if not uid:
                raise PermissionError(f"Odoo authentication failed for user '{self.username}'.")
            self._uid = int(uid)
            self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", allow_none=True)
        return self._uid, self._models

    def _execute(self, model: str, method: str, args: list[Any], kwargs: dict[str, Any] | None = None) -> Any:
        uid, models = self._connect()
        return models.execute_kw(self.db, uid, self.api_key, model, method, args, kwargs or {})

    # --- speaker -> res.users ----------------------------------------------------------------
    def fetch_active_users(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        if self._users_cache is None or force_refresh:
            self._users_cache = self._execute(
                "res.users", "search_read",
                [[["active", "=", True], ["share", "=", False]]],
                {"fields": ["id", "name", "login", "email"]})
        return self._users_cache

    def resolve_speaker_to_user_id(self, speaker_name: str, min_similarity: float = 0.78) -> int | None:
        cleaned = re.sub(r"[\[\]\*`]", "", speaker_name).strip()  # keep "@": labels may be logins
        if cleaned.lower() in UNASSIGNED:
            return None
        users = self.fetch_active_users()
        target = self.speaker_aliases.get(cleaned.lower(), cleaned).lower()

        for u in users:  # 1. exact name / login / email
            if target in {(u.get("name") or "").strip().lower(), (u.get("login") or "").strip().lower(),
                          (u.get("email") or "").strip().lower()}:
                return int(u["id"])

        first = []  # 2. unique first-name or email local-part
        for u in users:
            tokens = (u.get("name") or "").strip().lower().split()
            prefix = (u.get("login") or "").split("@")[0].strip().lower()
            if target == prefix or (tokens and target == tokens[0]):
                first.append(int(u["id"]))
        if len(first) == 1:
            return first[0]

        best_id, best = None, 0.0  # 3. fuzzy against full name and each token
        for u in users:
            name = (u.get("name") or "").strip().lower()
            score = max((SequenceMatcher(None, target, c).ratio() for c in [name, *name.split()] if c), default=0.0)
            if score > best:
                best, best_id = score, int(u["id"])
        if best_id is not None and best >= min_similarity:
            logger.info("Fuzzy-matched speaker %r -> res.users %s (%.2f)", speaker_name, best_id, best)
            return best_id
        logger.warning("Could not resolve speaker %r to an Odoo user.", speaker_name)
        return None

    def map_speakers(self, names: list[str]) -> dict[str, int | None]:
        return {n: self.resolve_speaker_to_user_id(n) for n in dict.fromkeys(names)}

    # --- markdown helpers ---------------------------------------------------------------------
    @staticmethod
    def parse_action_items_from_markdown(markdown_text: str) -> list[ActionItem]:
        m = re.search(r"##\s*Action Items\s*\n(.*?)(?=\n##\s|\Z)", markdown_text, re.IGNORECASE | re.DOTALL)
        if not m:
            return []
        items: list[ActionItem] = []
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 2:
                continue
            head = re.sub(r"[*`]", "", cells[0]).lower()
            if head in {"owner", "assignee", "who"} or set(cells[0]) <= {"-", ":", " "}:
                continue
            owner = re.sub(r"[*`\[\]]", "", cells[0]).strip()
            task = re.sub(r"[*`]", "", cells[1]).strip()
            deadline = cells[2].strip() if len(cells) >= 3 and cells[2].strip() else None
            if task and not set(task) <= {"-", ":"}:
                items.append(ActionItem(owner=owner, task=task, deadline=deadline))
        return items

    @staticmethod
    def _normalize_deadline(raw: str | None) -> str | None:
        if not raw:
            return None
        cleaned = re.sub(r"[*`]", "", raw).strip()
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y"):
            try:
                return datetime.strptime(cleaned, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    @staticmethod
    def _markdown_to_basic_html(markdown_text: str) -> str:
        out: list[str] = []
        in_list = False

        def bold(s: str) -> str:
            return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)

        for line in markdown_text.splitlines():
            s = line.strip()
            if not s or s.startswith("|"):  # tables are re-created as sub-tasks
                if in_list:
                    out.append("</ul>")
                    in_list = False
                continue
            if s.startswith("## "):
                if in_list:
                    out.append("</ul>")
                    in_list = False
                out.append(f"<h3>{s[3:]}</h3>")
            elif s.startswith(("- ", "* ")):
                if not in_list:
                    out.append("<ul>")
                    in_list = True
                out.append(f"<li>{bold(s[2:])}</li>")
            else:
                if in_list:
                    out.append("</ul>")
                    in_list = False
                out.append(f"<p>{bold(s)}</p>")
        if in_list:
            out.append("</ul>")
        return "\n".join(out)

    # --- publish -------------------------------------------------------------------------------
    def publish_meeting_with_subtasks(self, meeting_id: str, consolidated_markdown: str,
                                      participants: list[str], project_id: int | None = None) -> dict[str, Any]:
        target_project_id = project_id or self.project_id
        speaker_user_map = self.map_speakers(participants)
        participant_ids = sorted({u for u in speaker_user_map.values() if u is not None})
        meeting_date = datetime.now().strftime("%Y-%m-%d")

        parent_vals: dict[str, Any] = {
            "name": f"[Meeting Notes] {meeting_id} ({meeting_date})",
            "description": self._markdown_to_basic_html(consolidated_markdown),
        }
        if target_project_id:
            parent_vals["project_id"] = target_project_id
        if participant_ids:
            parent_vals["user_ids"] = [(6, 0, participant_ids)]
        parent_task_id = int(self._execute("project.task", "create", [parent_vals]))

        subtasks = []
        for item in self.parse_action_items_from_markdown(consolidated_markdown):
            owner_uid = self.resolve_speaker_to_user_id(item.owner)
            vals: dict[str, Any] = {
                "name": item.task if owner_uid is not None else f"[{item.owner}] {item.task}",
                "parent_id": parent_task_id,
                "description": (f"<p><strong>Assigned Speaker:</strong> {item.owner}</p>"
                                f"<p><strong>Source Meeting:</strong> {meeting_id} ({meeting_date})</p>"),
            }
            if target_project_id:
                vals["project_id"] = target_project_id
            if owner_uid is not None:
                vals["user_ids"] = [(6, 0, [owner_uid])]
            deadline = self._normalize_deadline(item.deadline)
            if deadline:
                vals["date_deadline"] = deadline
            sub_id = int(self._execute("project.task", "create", [vals]))
            subtasks.append({"subtask_id": sub_id, "owner": item.owner, "odoo_user_id": owner_uid,
                             "task": item.task, "deadline": deadline})
        return {"status": "published", "parent_task_id": parent_task_id,
                "speaker_user_map": speaker_user_map, "subtasks": subtasks}

    def publish_or_save(self, meeting_id: str, markdown: str, participants: list[str], out_dir: Path) -> dict[str, Any]:
        """Publish to Odoo when configured and reachable; the Markdown is always saved locally first."""
        path = Path(out_dir) / f"{meeting_id}.minutes.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown, encoding="utf-8")
        if not self.is_configured:
            return {"status": "saved_locally", "path": str(path), "reason": "odoo not configured"}
        try:
            result = self.publish_meeting_with_subtasks(meeting_id, markdown, participants)
        except Exception as e:  # network, auth, access rights: never lose the minutes
            logger.exception("Odoo publish failed")
            return {"status": "saved_locally", "path": str(path), "reason": f"{type(e).__name__}: {e}"}
        result["path"] = str(path)
        return result
