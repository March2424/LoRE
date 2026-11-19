"""Session state and non-secret local CLI configuration."""

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any


def default_config_path() -> Path:
    custom = os.environ.get("LORE_CLI_CONFIG")
    return Path(custom) if custom else Path.home() / ".config" / "lore-cli" / "config.json"


@dataclass
class SessionState:
    """Active CLI workspace context; never stores plaintext API keys on disk."""

    api_url: str = "http://127.0.0.1:8766"
    project_id: str = "naev"
    project_name: str = "Naev (default)"
    upload_id: str | None = None
    upload_filename: str | None = None
    import_id: str | None = None
    import_filename: str | None = None
    selected_ids: list[str] = field(default_factory=list)
    selected_labels: dict[str, str] = field(default_factory=dict)
    active_job_id: str | None = None
    active_plan_id: str | None = None
    active_candidate_id: str | None = None
    choices: dict[str, str] = field(default_factory=dict)
    target_lang: str = "zh"
    last_entries: list[dict[str, Any]] = field(default_factory=list)
    event_cursors: dict[str, int] = field(default_factory=dict)
    session_env: dict[str, str] = field(default_factory=dict)
    spawned: bool = False
    spawn_args: dict[str, Any] = field(default_factory=dict)

    def clear_selection(self):
        """Reset selected occurrence IDs."""
        self.selected_ids.clear()
        self.selected_labels.clear()

    def set_project(self, project_id: str, name: str):
        """Switch active project and clear project-scoped selections."""
        if project_id != self.project_id:
            self.project_id = project_id
            self.project_name = name
            self.upload_id = None
            self.upload_filename = None
            self.import_id = None
            self.import_filename = None
            self.clear_selection()
            self.last_entries.clear()
            self.choices.clear()


def load_cli_config(path: Path | None = None) -> dict[str, Any]:
    """Read saved non-secret CLI preferences if present."""
    target = path or default_config_path()
    if not target.is_file():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cli_config(state: SessionState, path: Path | None = None):
    """Persist only non-secret connection and project selection settings."""
    target = path or default_config_path()
    payload = {
        "api_url": state.api_url,
        "project_id": state.project_id,
        "project_name": state.project_name,
        "target_lang": state.target_lang,
    }
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
