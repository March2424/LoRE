"""Resolve structured Pi intent against the server's authorized resource catalog."""

from localization_agent.models.structured import parse_json_reply

INTENTS = ("translate", "review", "propose_update", "explain", "export")
LANGUAGES = {
    "zh": {"zh", "zh-hans", "zh-cn", "中文", "简体中文", "chinese", "simplified chinese"},
    "en": {"en", "en-us", "en-gb", "英文", "英语", "english"},
}
INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intents": {"type": "array", "items": {"type": "string", "enum": list(INTENTS)}},
        "targets": {"type": "array", "items": {"type": "string"}},
        "target_lang": {"type": ["string", "null"]},
        "requirements": {"type": ["string", "null"]},
        "term_source": {"type": ["string", "null"]},
        "term_target": {"type": ["string", "null"]},
        "missing": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
    "required": ["intents", "targets", "target_lang", "requirements", "term_source", "term_target", "missing"],
}


def normalize(text):
    return " ".join(text.casefold().split())


def language(value):
    name = normalize(value or "")
    return next((code for code, names in LANGUAGES.items() if name in names), None)


def scope_resources(catalog, selection):
    return {
        key: item for key, item in catalog.items()
        if (not selection.get("file") or item["file"] == selection["file"])
        and (not selection.get("branch_id") or item["branch_id"] == selection["branch_id"])
        and (not selection.get("source_ids") or key in selection["source_ids"])
    }


def resolve(catalog, request, parsed, additions, *, text_id=None, project=None):
    inputs = {**request, **additions}
    scope = request["selection"]
    available = scope_resources(catalog, scope)
    intents = inputs.get("intents") or parsed.get("intents") or []
    findings = {}
    if not intents or any(intent not in INTENTS for intent in intents):
        findings["intent"] = {"reason": "choose_supported_intent", "choices": list(INTENTS)}
    intents = list(dict.fromkeys(intent for intent in intents if intent in INTENTS))
    if "translate" in intents and "export" in intents:
        # Export is always dependent on translation and any requested additional review.
        intents.remove("export")
        intents.append("export")

    source_input = inputs.get("source_lang")
    target_input = inputs.get("target_lang") or parsed.get("target_lang") or scope.get("target_lang")
    source, target = language(source_input), language(target_input)
    opposite = {"en": "zh", "zh": "en"}
    if text_id:
        source = source or (opposite.get(target) if not source_input else None)
        target = target or (opposite.get(source) if not target_input else None)
        if not target or target == source:
            findings["target_lang"] = {"reason": "choose_other_supported_language", "choices":
                                       [opposite[source]] if source else list(LANGUAGES)}
        if source_input and not source:
            findings["source_lang"] = {"reason": "choose_supported_source_language", "choices": list(LANGUAGES)}
    elif project:
        source, target = project["source_lang"], project["target_lang"]
        if source_input and language(source_input) != source:
            findings["source_lang"] = {"reason": "project_source_language_mismatch", "choices": [source]}
        if target_input and language(target_input) != target:
            findings["target_lang"] = {"reason": "project_target_language_mismatch", "choices": [target]}
    else:
        if source_input and source != "en":
            findings["source_lang"] = {"reason": "naev_source_language_is_english", "choices": ["en"]}
        if (target_input or any(intent in intents for intent in ("translate", "review"))) and target != "zh":
            findings["target_lang"] = {"reason": "naev_requires_chinese_target", "choices": ["zh"]}
        source, target = "en", "zh"

    # A pasted source is the sole resource authorized for the task.
    requested = [] if text_id else inputs.get("targets") or parsed.get("targets") or []
    if not text_id and not requested and (scope.get("source_ids") or scope.get("file") or scope.get("branch_id")):
        requested = scope.get("source_ids") or list(available)
    ids = [text_id] if text_id else []
    for requested_resource in requested:
        query = normalize(requested_resource)
        matches = [key for key, item in available.items()
                   if query in {normalize(key), normalize(item["label"]), normalize(item["file"] or "")}]
        if len(matches) != 1:
            findings[requested_resource] = {"reason": "ambiguous_target" if matches else "target_not_in_scope",
                                            "choices": (matches or list(available))[:40]}
        else:
            ids.append(matches[0])
    if not ids and not requested:
        findings["targets"] = {"reason": "target_required", "choices": list(available)[:40]}
    limit = 64 if project else 12
    if len(set(ids)) > limit:
        findings["targets"] = {"reason": f"choose_at_most_{limit}_occurrences", "choices": ids[:limit]}

    terms = inputs.get("term") or {key: parsed.get(f"term_{key}") for key in ("source", "target")}
    if "propose_update" in intents and not all(isinstance(terms.get(key), str) and terms[key].strip()
                                                for key in ("source", "target")):
        findings["term"] = {"reason": "source_and_target_terms_required"}
    drafts = inputs.get("drafts", {})
    if text_id and "source_text" in drafts:
        drafts = {**drafts, text_id: drafts["source_text"]}
    return {
        "intents": intents, "targets": list(dict.fromkeys(ids)), "source_lang": source, "target_lang": target,
        "requirements": inputs.get("requirements") or parsed.get("requirements"),
        "term": terms, "drafts": drafts,
    }, findings


def parse_intent(text):
    result = parse_json_reply(text)
    if not isinstance(result, dict) or result.keys() != INTENT_SCHEMA["properties"].keys():
        raise ValueError("intent_schema")
    for key in ("intents", "targets", "missing"):
        values = result[key]
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError("intent_schema")
    if any(result[key] is not None and not isinstance(result[key], str)
           for key in ("target_lang", "requirements", "term_source", "term_target")):
        raise ValueError("intent_schema")
    return result


def fixture_intent(text):
    """Deterministic offline routing fixture; Python still resolves every resource reference."""
    lower = normalize(text)
    markers = {"translate": ("翻译", "translate", "中译英", "英译中"), "review": ("审校", "review"),
               "propose_update": ("术语", "glossary"), "explain": ("解释", "explain"),
               "export": ("导出", "export")}
    target = "en" if any(word in lower for word in ("中译英", "成英文", "成英语", "至英文", "into english", "to english", " en")) else (
        "zh" if any(word in lower for word in ("英译中", "中文", "chinese", " zh")) else None)
    return {"intents": [intent for intent, words in markers.items() if any(word in lower for word in words)],
            "targets": [], "target_lang": target,
            "requirements": None, "term_source": None, "term_target": None, "missing": []}
