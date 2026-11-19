"""Read-only reviewer tools and replayable ContextPack extension."""

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import re
from time import monotonic

from localization_agent.retrieval.game_index import normalize_name
from localization_agent.retrieval.service import GraphRetriever, candidate_from_memory, canonical
from localization_agent.schemas.graph import Candidate, ContextPack
from localization_agent.schemas.memory import Evidence, MemoryRef, Occurrence, is_mandatory, scope_match, text_hash

from .policy import expand_query


TOOL = "search_source_graph"
GET_EVIDENCE = "get_evidence"
GLOSSARY, TM, CHARACTER = "lookup_glossary", "search_tm", "get_character_note"
MEMORY_TOOLS = {GLOSSARY: "term", TM: "translation", CHARACTER: "style"}
REVIEW_RELATIONS = frozenset({"REVIEW_SOURCE_SEARCH", GET_EVIDENCE, *MEMORY_TOOLS})
ISSUES = {"ambiguity", "missing_evidence", "terminology", "context"}
KINDS = {"entity", "relation", "resource"}


def words(text):
    return " " + " ".join(re.findall(r"[^\W_]+", text.casefold())) + " "


@dataclass(frozen=True)
class SourceQuery:
    issue: str
    kind: str
    query: str

    def __post_init__(self):
        if (self.issue not in ISSUES or self.kind not in KINDS or not isinstance(self.query, str)
                or not 1 <= len(self.query.strip()) <= 120):
            raise ValueError("invalid_source_query")


class ScopedReviewerRetriever(GraphRetriever):
    """One item and a server-authorized source scope per CLI worker."""

    def __init__(self, graph, memory, config, *, token_counter, initial_ids, review_ids, provenance,
                 memory_ids=None, current_tm=None):
        super().__init__(graph, memory, config, token_counter=token_counter, allowed_source_ids=initial_ids)
        self.review_ids = review_ids
        self.memory_ids = review_ids if memory_ids is None else memory_ids
        self.current_tm = current_tm
        self.provenance = provenance
        self.supplement = None

    def _source_allowed_for_review(self, current: Occurrence, candidate: Occurrence, ids=None) -> bool:
        return (
            candidate.occurrence_id in (self.review_ids if ids is None else ids)
            and (candidate.project_id, candidate.source_lang, candidate.target_lang)
            == (current.project_id, current.source_lang, current.target_lang)
            and all(candidate.metadata.get(key) == current.metadata.get(key) for key in ("branch_id", "phase_id"))
            and (not candidate.metadata.get("requirements")
                 or candidate.metadata["requirements"] == current.metadata.get("requirements"))
        )

    @staticmethod
    def _resolved(current, pool):
        return replace(current, metadata={**current.metadata, **pool.resolved_metadata})

    def memory_sources(self, current, record):
        store = self.memory.store
        if (record.status != "approved" or not record.reviewer or not store.is_valid(record.ref)
                or record.content.get("verification") == "unreviewed" or scope_match(record.scope, current) != "match"):
            return ()
        if record.kind == "translation":
            check = record.content.get("check", {})
            primary = record.evidence[0]
            if (record.content.get("verification") != "reviewed" or not check.get("passed")
                    or (check.get("occurrence_id"), check.get("source_version")) != (primary.occurrence_id, primary.source_version)
                    or check.get("target_hash") != text_hash(record.content["target"])
                    or not check.get("context_hash") or primary.occurrence_id == current.occurrence_id
                    or not store.game_compatible(record, current) or (self.current_tm and not self.current_tm(record))):
                return ()
        if record.kind == "style" and current.metadata.get("speaker_id") not in record.scope.conditions.get("speaker_id", ()):
            return ()
        if record.kind == "term" and record.content.get("glossary_rule"):
            # A P4 rule can cover several occurrences. Cite its applicability at the current source.
            if record.content["source"] in current.source:
                return (Evidence(current.occurrence_id, current.source_version, record.content["source"]),)
            return ()
        if all(self._source_allowed_for_review(current, store.source(current.project_id, e), self.memory_ids)
               for e in record.evidence):
            return record.evidence
        return ()

    def lookup_memory(self, current, pack, name, query):
        current = self._resolved(current, pack.pool)
        if name == CHARACTER and not current.metadata.get("speaker_id"):
            return "speaker_unresolved", [], 0
        normalize = normalize_name if current.source_lang == "zh" else words
        terms = normalize(query) if name == TM else None
        hits, outside = [], 0
        for record in self.memory.eligible(current, pack.pool.view):
            if record.kind != MEMORY_TOOLS[name]:
                continue
            content = record.content
            if name == GLOSSARY and normalize_name(content["source"]) != normalize_name(query):
                continue
            if name == TM and (not terms.strip() or terms not in normalize(content["source"])):
                continue
            sources = self.memory_sources(current, record)
            if not sources:
                outside += 1
                continue
            mandatory = name == GLOSSARY and is_mandatory(record) and content["source"] in current.source
            candidate = candidate_from_memory(record, mandatory=mandatory, relations=(name,))
            # Do not copy P4's internal member list into the model's rendered context.
            if mandatory and content.get("glossary_rule"):
                candidate = replace(candidate, text=f'Source: {content["source"]}\nMandatory term: {content["target"]}')
            candidate = replace(candidate, sources=sources)
            first = sources[0]
            fields = ({"speaker_id": current.metadata["speaker_id"], "note": content["note"]}
                      if name == CHARACTER else {"source": content["source"], "target": content["target"]})
            hits.append((candidate, {"kind": record.kind, "memory_ref": asdict(record.ref),
                                     "occurrence_id": first.occurrence_id, "source_version": first.source_version,
                                     "location": self.provenance.get(first.occurrence_id) or {"occurrence_id": first.occurrence_id},
                                     **fields, **({"mandatory": mandatory} if name == GLOSSARY else {})}))
        if name == GLOSSARY and len({details["target"] for candidate, details in hits if candidate.mandatory}) > 1:
            return "evidence_conflict", [], outside
        hits.sort(key=lambda hit: (not hit[0].mandatory, name == TM and normalize(hit[1]["source"]) != terms, hit[0].id))
        return ("found" if hits else "out_of_scope" if outside else "no_match"), hits, outside

    def receipt_allowed(self, current, pack, entry):
        source = Evidence(entry["occurrence_id"], entry["source_version"], "")
        if "memory_ref" not in entry:
            return self._source_allowed_for_review(current, self.memory.store.source(current.project_id, source))
        ref = MemoryRef(**entry["memory_ref"])
        effective = self._resolved(current, pack.pool)
        record = next((row for row in self.memory.eligible(effective, pack.pool.view) if row.ref == ref), None)
        return bool(record and any((e.occurrence_id, e.source_version) == (source.occurrence_id, source.source_version)
                                   for e in self.memory_sources(effective, record)))

    def _object_allowed(self, index, current, obj):
        scope = replace(obj.scope, document_id=None)
        return scope_match(scope, current) == "match" and all(
            self._source_allowed_for_review(current, index.sources[e.occurrence_id]) for e in obj.evidence
        )

    def _matches(self, index, current, query):
        term = normalize_name(query.query)
        if query.kind == "entity":
            for (entity_type, name), refs in index.names.items():
                if term == name:
                    for ref in refs:
                        node = index.nodes[ref]
                        for evidence in node.evidence:
                            yield evidence.occurrence_id, (node,), entity_type, None
        elif query.kind == "relation":
            # Start from the authorized resources and inspect direct and one-hop graph relations.
            for source_id in sorted(self.review_ids):
                start = index.occurrences.get(source_id)
                if start is None:
                    continue
                outgoing = index.outgoing.get(start, ())
                edges = {edge.ref: edge for edge in outgoing}
                for edge in outgoing:
                    entity = index.nodes[edge.target]
                    if entity.kind == "entity" and self._object_allowed(index, current, edge):
                        edges.update((link.ref, link) for link in index.outgoing.get(entity.ref, ()))
                for edge in edges.values():
                    if term == normalize_name(edge.relation):
                        ends = (index.nodes[edge.source], index.nodes[edge.target])
                        for evidence in edge.evidence:
                            yield evidence.occurrence_id, (edge, *ends), None, edge.relation
        else:
            ids = (term,) if term in index.occurrences else sorted(self.review_ids)
            for source_id in ids:
                node_ref = index.occurrences.get(source_id)
                if node_ref is None:
                    continue
                source = index.sources[source_id]
                path = self.provenance.get(source_id, {}).get("path", "")
                if term == source_id or term in normalize_name(source.source) or term in normalize_name(path):
                    yield source_id, (index.nodes[node_ref],), None, None

    def lookup(self, current, pack, query):
        index = self.game_index(pack.pool.graph_snapshot)
        if index.snapshot.project_id != current.project_id:
            raise ValueError("Reviewer graph snapshot belongs to another project")
        used = {e["occurrence_id"] for e in pack.manifest["sources"]} | {current.occurrence_id}
        hits, kinds = {}, set()
        outside = already_seen = 0
        for source_id, objects, entity_type, relation in self._matches(index, current, query):
            source = index.sources[source_id]
            if (not self._source_allowed_for_review(current, source)
                    or not all(self._object_allowed(index, current, obj) for obj in objects)):
                outside += 1
                continue
            if source_id in used:
                already_seen += 1
                continue
            if entity_type:
                kinds.add(entity_type)
            if source_id in hits:
                continue
            ref = index.occurrences[source_id]
            origin = self.provenance.get(source_id) or {"path": source.document_id, "position": source.position}
            details = {"occurrence_id": source_id, "source_version": source.source_version,
                       "location": origin, "snippet": source.source}
            if entity_type:
                details["entity_type"] = entity_type
            if relation:
                details["relation"] = relation
                details["quote"] = next(e.quote for e in objects[0].evidence if e.occurrence_id == source_id)
            candidate = Candidate(
                f"source:{source_id}@{source.source_version}", "source", canonical(details),
                sources=tuple(dict.fromkeys((Evidence(source_id, source.source_version, source.source),)
                                            + tuple(e for obj in objects for e in obj.evidence))),
                graph_refs=tuple(dict.fromkeys((ref, *(obj.ref for obj in objects)))),
                relations=("REVIEW_SOURCE_SEARCH", *((relation,) if relation else ())),
            )
            hits[source_id] = (candidate, details)
        if len(kinds) > 1:
            return "evidence_conflict", [], outside
        if not hits:
            status = "out_of_scope" if outside else "no_new_evidence" if already_seen else "no_match"
            return status, [], outside
        return "found", [hits[key] for key in sorted(hits)], outside

    def get_evidence(self, current, pack, evidence_id):
        candidate = next((item for item in pack.pool.candidates if item.id == evidence_id), None)
        if candidate is None:
            source_id, at, version = evidence_id.removeprefix("source:").rpartition("@")
            if not evidence_id.startswith("source:") or not at or not version.isascii() or not version.isdecimal():
                return "no_match", [], 0
            if source_id not in self.review_ids:
                return "out_of_scope", [], 1
            index = self.game_index(pack.pool.graph_snapshot)
            ref = index.occurrences.get(source_id)
            source = index.sources.get(source_id)
            if ref is None or source is None or version != str(source.source_version):
                return "no_match", [], 0
            if not self._object_allowed(index, current, index.objects[ref]):
                return "out_of_scope", [], 1
            candidate = Candidate(evidence_id, "source", source.source,
                                  sources=(Evidence(source_id, source.source_version, source.source),), graph_refs=(ref,))

        details = {"kind": candidate.category, "text": candidate.text}
        if candidate.memory_ref:
            effective = self._resolved(current, pack.pool)
            record = next((row for row in self.memory.eligible(effective, pack.pool.view)
                           if row.ref == candidate.memory_ref), None)
            sources = self.memory_sources(effective, record) if record else ()
            details["memory_ref"] = asdict(candidate.memory_ref)
        else:
            index = self.game_index(pack.pool.graph_snapshot)
            sources = candidate.sources if (all(ref in index.objects and self._object_allowed(index, current, index.objects[ref])
                                                for ref in candidate.graph_refs)
                                            and all(e.occurrence_id in index.sources
                                                    and index.sources[e.occurrence_id].source_version == e.source_version
                                                    and self._source_allowed_for_review(current, index.sources[e.occurrence_id])
                                                    for e in candidate.sources)) else ()
        if not sources:
            return "out_of_scope", [], 1
        primary = sources[0]
        location = self.provenance.get(primary.occurrence_id) or {"occurrence_id": primary.occurrence_id}
        candidate = replace(candidate, sources=sources, relations=candidate.relations + (GET_EVIDENCE,))
        return "found", [(candidate, {**details, "occurrence_id": primary.occurrence_id,
                                      "source_version": primary.source_version, "location": location})], 0

    @staticmethod
    def _extend_pool(pool, candidates):
        ids = {item.id for item in candidates}
        return replace(pool, candidates=tuple(item for item in pool.candidates if item.id not in ids) + candidates)

    def prepare_pack(self, pack, candidates):
        incoming = tuple(item.id for item in candidates)
        previous = [item.id for item in pack.selected if not item.mandatory and item.id not in incoming]
        pool = self._extend_pool(pack.pool, candidates)
        while True:
            try:
                return self.build_context(pool, incoming + tuple(previous))
            except ValueError:
                if not previous:
                    return None
                previous.pop()

    def activate(self, pack, candidates):
        """Retain the exact evidence that the repairer and last Reviewer will see."""
        self.supplement = (pack.pool.occurrence_id, pack.pool.source_version, pack.pool.view,
                           pack.pool.graph_snapshot, candidates)

    def retrieve(self, occurrence, view, snapshot_id):
        pool = super().retrieve(occurrence, view, snapshot_id)
        if self.supplement is None:
            return pool
        item, version, bound_view, bound_snapshot, candidates = self.supplement
        if (occurrence.occurrence_id, occurrence.source_version, view, snapshot_id) != (
                item, version, bound_view, bound_snapshot):
            raise ValueError("Reviewer evidence belongs to a different source or read view")
        memory_candidates = [c for c in candidates if c.memory_ref]
        if memory_candidates:
            effective = self._resolved(occurrence, pool)
            records = {record.ref: record for record in self.memory.eligible(effective, view)}
            if any(c.memory_ref not in records or self.memory_sources(effective, records[c.memory_ref]) != c.sources
                   for c in memory_candidates):
                raise ValueError("Reviewer memory evidence is stale or out of scope")
        index = self.game_index(snapshot_id)
        if any(ref not in index.objects or not self._object_allowed(index, occurrence, index.objects[ref])
               for candidate in candidates for ref in candidate.graph_refs):
            raise ValueError("Reviewer evidence is stale or out of scope")
        return self._extend_pool(pool, candidates)


class SourceReview:
    def __init__(self, retriever: ScopedReviewerRetriever, current: Occurrence, pack: ContextPack, limits, trace, policy=None):
        self.retriever, self.current, self.pack = retriever, current, pack
        self.limits, self.trace = limits, trace
        self.policy = policy or {}
        self.attempted = False
        self.name = None
        self.status = None
        self.candidates = ()
        self.evidence_ids = []
        self.repacked = None

    def call(self, name, args, tool_id):
        started = monotonic()
        used_bytes = outside = 0
        accepted = []
        effective_query = None
        if self.attempted:
            status = "lookup_budget_exhausted"
        else:
            self.attempted = True
            self.name = name
            try:
                if name == TOOL:
                    query = SourceQuery(**args)
                    effective_query = expand_query(self.policy, query.issue, query.kind, query.query)
                    query = replace(query, query=effective_query)
                elif name == CHARACTER and args == {}:
                    effective_query = self.retriever._resolved(self.current, self.pack.pool).metadata.get("speaker_id")
                elif (name == GET_EVIDENCE and isinstance(args, dict) and args.keys() == {"evidence_id"}
                      and isinstance(args["evidence_id"], str) and 1 <= len(args["evidence_id"].strip()) <= 200):
                    effective_query = args["evidence_id"].strip()
                elif (name in {GLOSSARY, TM} and isinstance(args, dict) and args.keys() == {"query"}
                      and isinstance(args["query"], str) and 1 <= len(args["query"].strip()) <= 120):
                    effective_query = args["query"].strip()
                else:
                    raise ValueError("invalid tool or query")
            except (TypeError, ValueError):
                status = "invalid_query"
            else:
                if name == TOOL:
                    status, hits, outside = self.retriever.lookup(self.current, self.pack, query)
                elif name == GET_EVIDENCE:
                    status, hits, outside = self.retriever.get_evidence(self.current, self.pack, effective_query)
                else:
                    status, hits, outside = self.retriever.lookup_memory(self.current, self.pack, name, effective_query)
                current_ref = f"source:{self.current.occurrence_id}@{self.current.source_version}"
                selected = {c.id for c in self.pack.selected} | {current_ref}
                required = set()
                for candidate, details in hits:
                    if len(accepted) == self.limits["max_results"]:
                        break
                    evidence = [item[1] for item in accepted] + [{"evidence_id": candidate.id, **details}]
                    size = len(canonical({"status": "found", "evidence": evidence}).encode("utf-8"))
                    if size > self.limits["max_result_bytes"]:
                        continue
                    candidates = self.candidates + (candidate,) if candidate.id not in selected else self.candidates
                    updated = self.retriever.prepare_pack(self.pack, candidates) if candidates else self.pack
                    supported = required | {candidate.id}
                    if (updated is not None and updated.status == "ready"
                            and supported <= ({c.id for c in updated.selected} | {current_ref})):
                        accepted.append((candidate, evidence[-1]))
                        self.candidates, self.repacked = candidates, updated if candidates else None
                        required = supported
                        used_bytes = size
                if status == "found" and not accepted:
                    status = "evidence_budget_exhausted"
        self.status = status
        self.evidence_ids = [item[0].id for item in accepted]
        result = {"status": status, "evidence": [item[1] for item in accepted]}
        run_id = self.pack.pool.view.run_id
        self.trace("review_tool", item=self.current.occurrence_id, name=name, tool_id=tool_id, args=args,
                   effective_query=effective_query,
                   result=result, scope={"project": self.current.project_id,
                                         "authorized_source_ids": sorted(self.retriever.review_ids),
                                         "authorized_memory_source_ids": sorted(self.retriever.memory_ids),
                                         "branch_id": self.current.metadata.get("branch_id"),
                                         "phase_id": self.current.metadata.get("phase_id")},
                   graph_snapshot=self.pack.pool.graph_snapshot, view=asdict(self.pack.pool.view),
                   knowledge_snapshot=self.retriever.memory.store.run(run_id)["snapshot"],
                   excluded_hits=outside, budget={**self.limits, "used_result_bytes": used_bytes, "max_lookups": 1},
                   elapsed_ms=round(1000 * (monotonic() - started)))
        return result


def read_provenance(path: Path, allowed_ids):
    """Read only native source locations used by this batch."""
    provenance = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            if row["record_id"] in allowed_ids:
                provenance[row["record_id"]] = {
                    key: row["source"][key] for key in ("path", "line", "xpath") if key in row["source"]
                }
    return provenance
