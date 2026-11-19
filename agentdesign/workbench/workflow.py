"""LangGraph workflow over the research package's retrieval and version store."""

from collections import Counter
from dataclasses import asdict
import json
import re
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from localization_agent.memory.store import VersionConflict
from localization_agent.models.structured import parse_json_reply
from localization_agent.retrieval.service import GraphRetriever
from localization_agent.schemas.graph import CandidatePool, ContextPack
from localization_agent.schemas.memory import Occurrence, TranslationCheck, text_hash
from localization_agent.workflow.game import GameSession

from .evidence import TOOL, ScopedReviewerRetriever, SourceReview
from .models import SELECTION_SCHEMA, ModelFailure, Responses, parse_review

NAEV_MARKERS = re.compile(r"##|#[0-9A-Za-z]")
STOP = END


def budget_units(text: str) -> int:
    """UTF-8 bytes as a conservative proxy for soft evidence tokens."""
    return len(text.encode("utf-8"))


budget_units.name = "utf8-bytes/1"


def hard_problems(retriever, source, target: str, pack: ContextPack) -> list[str]:
    placeholders = GameSession(retriever).placeholders
    problems = []
    if not target.strip():
        problems.append("Empty translation")
    if Counter(placeholders(source.source)) != Counter(placeholders(target)):
        problems.append("Placeholder or tag set differs from source")
    if NAEV_MARKERS.findall(source.source) != NAEV_MARKERS.findall(target):
        problems.append("Naev control markers differ from source")
    if source.source.count("\n") != target.count("\n"):
        problems.append("Line break count differs from source")
    if outer_whitespace(source.source) != outer_whitespace(target):
        problems.append("Leading or trailing whitespace differs from source")
    for candidate in pack.selected:
        if candidate.mandatory and candidate.category == "term":
            term = retriever.memory.store.get(candidate.memory_ref).content
            if term["target"] not in target:
                problems.append(f'Mandatory term missing: {term["source"]} -> {term["target"]}')
    return problems


def outer_whitespace(text):
    return text[:len(text) - len(text.lstrip())], text[len(text.rstrip()):]


def localized_output(source, output):
    before, after = outer_whitespace(source)
    return before + output.strip() + after


class TaskState(TypedDict, total=False):
    target: str
    pool: CandidatePool
    pack: ContextPack
    problems: list[str]
    reviewer_issues: list[str]
    review_call: str
    revisions: int
    route: Literal["translate", "repair", "check", "commit", "__end__"]
    status: str
    reason: str
    version: dict


class TranslationAgent:
    def __init__(self, retriever: GraphRetriever, models: Responses, prompts, trace):
        self.retriever = retriever
        self.store = retriever.memory.store
        self.models, self.prompts, self.trace = models, prompts, trace
        self.placeholders = GameSession(retriever).placeholders

    def prompt(self, role, data):
        return self.prompts[role] + "\nResource payload:\n" + json.dumps(data, ensure_ascii=False)

    def selector(self, pool: CandidatePool, hints: tuple[str, ...], *, item, fixture):
        """Implement the PR #8 selector shape, with a trace of the raw and applied action."""
        observation = self.retriever.selector_observation(pool)
        choices = dict(zip((entry["id"] for entry in observation["candidates"]),
                           (candidate.id for candidate in pool.candidates if not candidate.mandatory)))
        decision = None
        call_id = None
        try:
            raw, call_id = self.models.ask(
                "selector", self.prompt("selector", {**observation, "review_hints": hints}),
                item=item, fixture=fixture, schema=SELECTION_SCHEMA,
            )
            decision = parse_json_reply(raw)
            if (
                not isinstance(decision, dict) or set(decision) != {"selected_ids", "rationale"}
                or not isinstance(decision["rationale"], str) or not isinstance(decision["selected_ids"], list)
                or any(not isinstance(i, str) or i not in choices for i in decision["selected_ids"])
            ):
                raise ValueError("selector_schema_or_unknown_id")
            pack = self.retriever.build_context(pool, tuple(choices[i] for i in decision["selected_ids"]))
            fallback = None
        except (ModelFailure, ValueError) as error:
            if isinstance(error, ModelFailure):
                if error.code in {"unknown_provider_result", "call_input_changed", "stale_tool_receipt"}:
                    raise
                call_id = error.call_id
            pack = self.retriever.build_context(pool)
            fallback = error.code if isinstance(error, ModelFailure) else str(error)
        self.trace("selection", item=item, call_id=call_id, decision=decision, fallback=fallback,
                   applied_ids=[c.id for c in pack.selected if not c.mandatory],
                   actual_context_pack=asdict(pack))
        return pack

    def run(self, source: Occurrence, run_id: str, snapshot_id: str, case: dict, fixture: dict, *,
            lookup_limits=None, defer_commit=False, review_target=None, read_view=None, policy=None):
        item = source.occurrence_id
        review_attempt = 0
        first_review = None
        last_target = ""
        lookup_status = None
        latest_pack = None

        def payload(state):
            pack = state["pack"]
            return {
                "source": source.source, "source_lang": source.source_lang, "target_lang": source.target_lang,
                "current_resource": json.loads(pack.manifest["current_source_context"]),
                "selected_evidence": json.loads(pack.model_context),
                "editor_requirement": case.get("editor_requirement"),
            }

        def retrieve(state):
            pool = self.retriever.retrieve(source, read_view or self.store.view(run_id), snapshot_id)
            self.trace("retrieval", item=item, candidate_pool=asdict(pool))
            return {"pool": pool}

        def select(state):
            nonlocal latest_pack
            pack = self.selector(state["pool"], (), item=item, fixture=fixture.get("selector"))
            latest_pack = pack
            if pack.status != "ready":
                return {"pack": pack, "route": STOP, "status": "conflict",
                        "reason": ", ".join(pack.pool.conflicts) or pack.status}
            return {"pack": pack, "route": "translate"}

        def translate(state):
            nonlocal last_target
            if review_target is not None:
                last_target = review_target
                return {"target": review_target}
            raw, call = self.models.ask(
                "translator", self.prompt("translator", payload(state)), item=item, fixture=fixture.get("translator"),
            )
            target = localized_output(source.source, raw)
            if "injected_draft" in case:
                target = case["injected_draft"]
            elif case.get("fault") == "remove_first_placeholder":
                token = self.placeholders(source.source)[0]
                target = target.replace(token, "", 1)
            last_target = target
            self.trace("draft", item=item, call_id=call, target=target, generated=raw.strip(),
                       injection=case.get("fault") or ("injected_draft" if "injected_draft" in case else None))
            return {"target": target}

        def check(state):
            target = state["target"]
            problems = hard_problems(self.retriever, source, target, state["pack"])
            self.trace("hard_check", item=item, target_hash=text_hash(target), problems=problems,
                       context_hash=state["pack"].context_hash)
            return {"problems": problems}

        def review(state):
            nonlocal review_attempt, first_review, lookup_status, latest_pack
            attempt = review_attempt
            review_attempt += 1
            pack, target = state["pack"], state["target"]
            review_input = {**payload(state), "draft": target, "hard_check": state["problems"]}
            tool = None
            if attempt == 0 and isinstance(self.retriever, ScopedReviewerRetriever):
                tool = SourceReview(self.retriever, source, pack, lookup_limits, self.trace, policy)
            template = "reviewer_source" if tool else "reviewer_recheck" if lookup_limits else "reviewer"
            try:
                receipt, call = self.models.pi(
                    self.prompt(template, review_input), item=item,
                    fixture=fixture["reviewer"][attempt] if self.models.offline else None,
                    tool=tool.call if tool else None,
                    mode="source_recheck" if lookup_limits and tool is None else "review",
                )
            except ModelFailure as error:
                if tool and tool.attempted:
                    lookup_status = tool.status
                    if lookup_status != "found":
                        return {"route": STOP, "status": "needs_human",
                                "reason": f"{lookup_status}: {error.code}"}
                raise
            binding = {"context_hash": pack.context_hash, "target_hash": text_hash(target)}
            if tool:
                lookup_status = tool.status or "lookup_not_requested"
                binding["tool_evidence_ids"] = tool.evidence_ids
            try:
                verdict = parse_review(receipt["text"])
            except ValueError:
                self.trace("review", item=item, call_id=call, **binding, error="review_structure")
                return {"route": STOP, "status": "failed", "reason": "review_structure"}
            if attempt == 0:
                first_review = verdict["verdict"]
            self.trace("review", item=item, call_id=call, **binding, **verdict)
            if tool and case.get("require_source_lookup") and tool.attempted and tool.name != TOOL:
                return {"route": STOP, "status": "needs_human", "reason": "source_lookup_required"}
            if tool and (tool.attempted or case.get("require_source_lookup")) and lookup_status != "found":
                return {"route": STOP, "status": "needs_human", "reason": f"{lookup_status}: {verdict['reason']}"}
            if verdict["verdict"] == "needs_human":
                return {"route": STOP, "status": "needs_human", "reason": verdict["reason"]}
            if case.get("require_evidence") and not any(not c.mandatory for c in pack.selected):
                return {"route": STOP, "status": "needs_human", "reason": "Requested source evidence unavailable"}
            changes = {}
            if tool and tool.repacked is not None:
                self.retriever.activate(pack, tool.candidates)
                latest_pack = tool.repacked
                self.trace("review_repack", item=item, review_call_id=call, previous_context_hash=pack.context_hash,
                           added_ids=[candidate.id for candidate in tool.candidates],
                           context_hash=tool.repacked.context_hash, manifest=tool.repacked.manifest)
                changes["pack"] = tool.repacked
            if state["problems"] or verdict["verdict"] == "repair":
                if state["revisions"] >= self.models.config["max_revisions"]:
                    return {"route": STOP, "status": "needs_human",
                            "reason": "Repair limit: " + "; ".join([*state["problems"], verdict["reason"]])}
                return {"route": "repair", "reviewer_issues": [verdict["reason"]], **changes}
            if changes:
                return {"route": "check", **changes}
            return {"route": "commit", "review_call": call}

        def repair(state):
            nonlocal last_target
            request = {**payload(state), "draft": state["target"],
                       "hard_check": state["problems"], "review_issues": state["reviewer_issues"]}
            raw, call = self.models.ask(
                "repairer", self.prompt("repairer", request), item=item, fixture=fixture.get("repairer"),
            )
            last_target = localized_output(source.source, raw)
            self.trace("repair", item=item, call_id=call, target=last_target)
            return {"target": last_target, "revisions": state["revisions"] + 1}

        def commit(state):
            pack, target = state["pack"], state["target"]
            check = TranslationCheck(item, source.source_version, text_hash(target), pack.context_hash,
                                     f'pi:tokendance:{state["review_call"]}', True)
            if defer_commit:
                return {"status": "passed", "version": {"check": asdict(check)}}
            record = self.retriever.commit(source, target, check, pack, expected_version=0)
            self.trace("commit", item=item, version=asdict(record.ref), check=asdict(check))
            return {"version": asdict(record.ref), "status": "committed"}

        graph = StateGraph(TaskState)
        for node in (retrieve, select, translate, check, review, repair, commit):
            graph.add_node(node.__name__, node)
        for start, end in ((START, "retrieve"), ("retrieve", "select"), ("translate", "check"),
                           ("check", "review"), ("repair", "check"), ("commit", END)):
            graph.add_edge(start, end)
        graph.add_conditional_edges("select", lambda s: s["route"], ["translate", STOP])
        graph.add_conditional_edges("review", lambda s: s["route"], ["commit", "repair", "check", STOP])
        try:
            final = graph.compile().invoke({"revisions": 0})
        except ModelFailure as error:
            budget_hit = error.code in {"call_budget_exhausted", "pi_response_incomplete:length",
                                        "pi_response_incomplete:max_output_tokens"}
            status = "needs_human" if lookup_limits and budget_hit else "failed"
            final = {"status": status, "reason": error.code}
        except VersionConflict as error:
            final = {"status": "conflict", "reason": str(error)}
        except (KeyError, ValueError) as error:
            final = {"status": "failed", "reason": f"{type(error).__name__}: {error}"}
        result = {"item": item, "source_version": source.source_version, "case": case["case"],
                  "status": final["status"], "reason": final.get("reason"), "target": last_target,
                  "version": final.get("version"), "first_review": first_review, "review_attempts": review_attempt,
                  "expected_first_review": case.get("expected_first_review"), "expected_status": case.get("expected_status")}
        if lookup_limits:
            result.update(lookup_status=lookup_status, expected_lookup_status=case.get("expected_lookup_status"))
        if defer_commit:
            result["check"] = (result.pop("version") or {}).get("check")
            result["pack"] = asdict(latest_pack) if latest_pack else None
        return result
