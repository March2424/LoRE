"""Versioned, narrowly scoped policy overlays used by the workbench."""

from copy import deepcopy
import json
from string import Formatter

from localization_agent.retrieval.game_index import normalize_name
from localization_agent.schemas.memory import text_hash
from .job_store import JobError, encode

SCHEMA = """
CREATE TABLE IF NOT EXISTS p5_feedback (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS p5_candidates (id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS p5_replays (id TEXT PRIMARY KEY, candidate TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS p5_releases (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, data TEXT NOT NULL);
"""
DEFAULT = {
    "prompts": {}, "handoff_template": "{reason}", "aliases": {}, "lookup_results": 2,
    "skill_issues": [], "memory_plan_id": None,
}
ISSUES = {"ambiguity", "missing_evidence", "terminology", "context"}


def versions(spec):
    components = {
        "prompt": [spec["prompts"], spec["handoff_template"]],
        "harness": [spec["aliases"], spec["lookup_results"]],
        "skill": {"workflow": "scoped_source_review", "issues": spec["skill_issues"]},
        "memory": spec["memory_plan_id"],
    }
    return {key: text_hash(encode(value)) for key, value in components.items()}


def active(repo):
    row = repo.db.execute("SELECT data FROM p5_releases ORDER BY seq DESC LIMIT 1").fetchone()
    if row:
        return json.loads(row[0])
    return {"release_id": "baseline", "spec": deepcopy(DEFAULT), "components": versions(DEFAULT), "parent": None}


def amended(base, changes):
    spec = deepcopy(base)
    for key, value in changes.items():
        if key in {"aliases", "prompts"}:
            spec[key].update(value)
        else:
            spec[key] = value
    if set(spec["prompts"]) - {"reviewer_source"} or set(spec["skill_issues"]) - ISSUES:
        raise JobError(422, "unsupported_policy_component")
    if any(not 1 <= len(text.strip()) <= 4000 for text in spec["prompts"].values()):
        raise JobError(422, "invalid_prompt_overlay")
    aliases = {}
    for before, after in spec["aliases"].items():
        if not 1 <= len(before.strip()) <= 120 or not 1 <= len(after.strip()) <= 120:
            raise JobError(422, "invalid_entity_alias")
        normalized = normalize_name(before)
        if normalized in aliases and aliases[normalized] != after.strip():
            raise JobError(422, "conflicting_entity_alias")
        aliases[normalized] = after.strip()
    spec["aliases"] = aliases
    template = spec["handoff_template"]
    try:
        fields = list(Formatter().parse(template))
    except ValueError as error:
        raise JobError(422, "invalid_handoff_template") from error
    if (not any(field == "reason" for _, field, _, _ in fields)
            or any(field is not None and (field not in {"reason", "occurrence_id"} or fmt or conversion)
                   for _, field, fmt, conversion in fields)):
        raise JobError(422, "invalid_handoff_template")
    return spec


def prompt_overrides(prompts, spec):
    return {role: text + ("\n\nEditor policy:\n" + spec["prompts"][role] if role in spec["prompts"] else "")
            for role, text in prompts.items()}


def handoff(spec, reason, occurrence_id):
    return spec["handoff_template"].format(reason=reason, occurrence_id=occurrence_id)


def expand_query(spec, issue, kind, query):
    if kind != "entity" or (spec.get("skill_issues") and issue not in spec["skill_issues"]):
        return query
    return spec.get("aliases", {}).get(normalize_name(query), query)
