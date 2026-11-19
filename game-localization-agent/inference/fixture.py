"""Deterministic replies for the two bundled smoke-test resources."""
import json

from localization_agent.models.protocols import ModelReply


def make_model():
    def call(task, prompt):
        evidence = lambda key: [{"id": key, "field": "source", "quote": "Emberblade"}]
        if task == "discover_graph":
            reply = json.dumps({
                "entities": [{"key": "blade", "name": "Emberblade", "entity_type": "item",
                              "reuse_id": None, "evidence": evidence("weapon")}],
                "relations": [
                    {"key": "defines", "source": "text:weapon", "target": "entity:blade",
                     "relation": "DEFINES", "conditions": {}, "evidence": evidence("weapon")},
                    {"key": "mentions", "source": "text:line", "target": "entity:blade",
                     "relation": "MENTIONS", "conditions": {}, "evidence": evidence("line")},
                ],
            })
        elif task == "teacher_select":
            candidates = json.loads(prompt.rsplit("\nObservation: ", 1)[1])["candidates"]
            reply = json.dumps({"rationale": "Use the known weapon name.",
                                "selected_ids": [candidates[0]["id"]] if candidates else []})
        elif task == "translate_game":
            reply = "拿上余烬剑，{player}。" if "\nSource: Take Emberblade, {player}.\n" in prompt else "余烬剑"
        elif task == "extract_terms":
            reply = '[{"source":"Emberblade","target":"余烬剑"}]'
        elif task == "summarize_topic":
            reply = "Emberblade is a weapon. 余烬剑是一把武器。"
        else:
            raise ValueError(f"Unexpected fixture task: {task}")
        return ModelReply(reply, "offline-fixture")

    return call
