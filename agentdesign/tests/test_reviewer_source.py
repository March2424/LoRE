import unittest
from dataclasses import replace

from run_p0 import CONFIG, PI, budget_units, load
from localization_agent.memory import MemoryService, MemoryStore
from localization_agent.retrieval.service import GraphConfig
from localization_agent.retrieval.graph import GraphStore
from localization_agent.schemas.graph import GraphEdge, GraphNode
from localization_agent.schemas.memory import Evidence, Occurrence, Scope, TranslationCheck, text_hash
from workbench.evidence import CHARACTER, GET_EVIDENCE, GLOSSARY, TM, ScopedReviewerRetriever, SourceQuery, SourceReview, TOOL
from workbench.models import Responses


class ReviewerSourceTests(unittest.TestCase):
    def setUp(self):
        store = self.store = MemoryStore()
        self.addCleanup(store.close)
        graph = GraphStore(store)
        self.rows = {}
        for key, branch, text, entity_type in (
            ("target", "route-a", "Pinnacle", None),
            ("in-scope", "route-a", "Pinnacle is the colony ship.", "item"),
            ("other", "route-b", "Pinnacle is a separate expedition.", "quest"),
        ):
            source = self.rows[key] = Occurrence("game", key, key, 0, text,
                                                 metadata={"branch_id": branch, "speaker_id": "pilot"})
            store.put_source(source)
            scope = Scope.for_occurrence(source)
            evidence = (Evidence(key, 1, text),)
            occurrence = graph.put_node(GraphNode(key, "occurrence", scope, {"occurrence_id": key}, evidence))
            if entity_type:
                entity = graph.put_node(GraphNode(key + "-entity", "entity", scope,
                                                  {"name": "Pinnacle", "entity_type": entity_type}, evidence))
                graph.put_edge(GraphEdge(key + "-edge", "DEFINES", occurrence.ref, entity.ref, scope, evidence,
                                         status="approved", reviewer="fixture"))
        graph.create_snapshot("graph", "game")
        store.create_snapshot("knowledge", "game")
        store.start_run("run", "game", None, "en", "zh", "knowledge", mode="game_resources")
        self.retriever = ScopedReviewerRetriever(
            graph, MemoryService(store), GraphConfig(), token_counter=budget_units,
            initial_ids=frozenset({"target"}), review_ids=frozenset(self.rows), provenance={},
        )
        self.source = self.rows["target"]
        self.view = store.view("run")
        self.pack = self.retriever.build_context(self.retriever.retrieve(self.source, self.view, "graph"))

    def test_same_name_other_branch_is_excluded_and_repack_replays(self):
        status, hits, excluded = self.retriever.lookup(
            self.source, self.pack, SourceQuery("ambiguity", "entity", "Pinnacle"))
        self.assertEqual((status, excluded), ("found", 1))
        self.assertEqual([details["occurrence_id"] for _, details in hits], ["in-scope"])
        candidates = tuple(candidate for candidate, _ in hits)
        repacked = self.retriever.prepare_pack(self.pack, candidates)
        self.assertNotEqual(repacked.context_hash, self.pack.context_hash)
        self.retriever.activate(self.pack, candidates)
        fresh = self.retriever.retrieve(self.source, self.view, "graph")
        rebuilt = self.retriever.build_context(fresh, tuple(item.id for item in repacked.selected))
        self.assertEqual(rebuilt.context_hash, repacked.context_hash)
        self.store.put_source(replace(self.rows["in-scope"], source_version=2))
        with self.assertRaisesRegex(ValueError, "stale"):
            self.retriever.retrieve(self.source, self.view, "graph")

    def test_query_type_no_hit_and_explicit_out_of_scope(self):
        for kind, query, expected in (
            ("resource", "in-scope", "found"),
            ("relation", "DEFINES", "found"),
            ("resource", "other", "out_of_scope"),
            ("entity", "Absent entity", "no_match"),
        ):
            with self.subTest(kind=kind, query=query):
                status, hits, _ = self.retriever.lookup(self.source, self.pack, SourceQuery("context", kind, query))
                self.assertEqual(status, expected)
                self.assertEqual([r["occurrence_id"] for _, r in hits], ["in-scope"] if expected == "found" else [])

    def test_tool_budget_stops_duplicate_and_oversized_results(self):
        review = SourceReview(self.retriever, self.source, self.pack, {"max_results": 1, "max_result_bytes": 120},
                              lambda *args, **kwargs: None)
        args = {"issue": "ambiguity", "kind": "entity", "query": "Pinnacle"}
        self.assertEqual(review.call(TOOL, args, "first"), {"status": "evidence_budget_exhausted", "evidence": []})
        self.assertEqual(review.call(TOOL, args, "second"), {"status": "lookup_budget_exhausted", "evidence": []})
        self.assertIsNone(review.repacked)

    def test_pi_get_evidence_resolves_exact_version_and_rejects_other_branch(self):
        limits = {"max_results": 2, "max_result_bytes": 1024}
        trace = lambda *args, **kwargs: None
        review = SourceReview(self.retriever, self.source, self.pack, limits, trace)
        fixture = {"tool": GET_EVIDENCE, "lookup": {"evidence_id": "source:in-scope@1"},
                   "result": {"verdict": "pass", "reason": "fixture"}}
        receipt, _ = Responses(load(CONFIG), trace, PI, offline=True).pi(
            "Review the fixture", item="target", tool=review.call, fixture=fixture)
        result, = [event["result"]["details"] for event in receipt["events"] if event["type"] == "tool_execution_end"]
        self.assertEqual(result["status"], "found")
        self.assertEqual(result["evidence"][0]["evidence_id"], "source:in-scope@1")

        for evidence_id, status in (("source:in-scope@2", "no_match"), ("source:other@1", "out_of_scope")):
            result = SourceReview(self.retriever, self.source, self.pack, limits, trace).call(
                GET_EVIDENCE, {"evidence_id": evidence_id}, "lookup")
            self.assertEqual(result, {"status": status, "evidence": []})
        existing = SourceReview(self.retriever, self.source, review.repacked, limits, trace)
        self.assertEqual(existing.call(GET_EVIDENCE, fixture["lookup"], "existing")["status"], "found")
        self.assertIsNone(existing.repacked)

    def test_pi_memory_tools_use_approved_scoped_evidence_at_the_read_watermark(self):
        store = self.store
        source = self.rows["in-scope"]
        evidence = (Evidence(source.occurrence_id, source.source_version, source.source),)
        scope = Scope("game", conditions={"branch_id": ("route-a",)})
        term = store.draft("term", scope, {"source": "Pinnacle", "target": "尖峰号"}, evidence)
        term = store.approve(term.ref, "editor")
        store.draft("term", scope, {"source": "Pinnacle", "target": "未批准译法"}, evidence)
        note = store.draft("style", replace(scope, conditions={**scope.conditions, "speaker_id": ("pilot",)}),
                           {"note": "语气简洁，使用驾驶术语。"}, evidence)
        note = store.approve(note.ref, "editor")
        pilot2 = store.draft("style", replace(scope, conditions={**scope.conditions, "speaker_id": ("other",)}),
                             {"note": "越权说话人"}, evidence)
        store.approve(pilot2.ref, "editor")
        store.create_snapshot("memory-tools", "game")
        store.start_run("memory-tools", "game", None, "en", "zh", "memory-tools", mode="game_resources")

        def commit(occurrence, target, *, reviewed=True):
            check = TranslationCheck(occurrence.occurrence_id, occurrence.source_version, text_hash(target),
                                     "fixture-context", "editor", reviewed)
            return store.commit_translation(occurrence, "memory-tools", target, check, allow_unreviewed=not reviewed)

        approved = commit(source, "尖峰号是殖民船。")
        commit(self.rows["other"], "另一分支的译文")
        unchecked = replace(source, occurrence_id="unchecked", source="Pinnacle unchecked")
        future = replace(source, occurrence_id="future", source="Pinnacle future")
        for occurrence in (unchecked, future):
            store.put_source(occurrence)
        self.retriever.memory_ids = self.retriever.memory_ids | {"unchecked", "future"}
        commit(unchecked, "未验证的译文", reviewed=False)
        view = store.view("memory-tools")
        commit(future, "晚于读水位的译文")
        pack = self.retriever.build_context(self.retriever.retrieve(self.source, view, "graph"), ())
        limits = {"max_results": 2, "max_result_bytes": 1024}

        for tool, args, record in ((GLOSSARY, {"query": "pinnacle"}, term),
                                   (TM, {"query": "Pinnacle"}, approved), (CHARACTER, {}, note)):
            with self.subTest(tool=tool):
                review = SourceReview(self.retriever, self.source, pack, limits, lambda *args, **kwargs: None)
                fixture = {"tool": tool, "lookup": args, "result": {"verdict": "pass", "reason": "fixture"}}
                models = Responses(load(CONFIG), lambda *args, **kwargs: None, PI, offline=True)
                receipt, _ = models.pi("Review the fixture", item="target", tool=review.call, fixture=fixture)
                result, = [event["result"]["details"] for event in receipt["events"] if event["type"] == "tool_execution_end"]
                returned, = result["evidence"]
                self.assertEqual(result["status"], "found")
                self.assertEqual(returned["memory_ref"], {"record_id": record.record_id, "version": record.version})
                self.assertTrue(self.retriever.receipt_allowed(self.source, pack, returned))
                self.assertEqual(review.call(TM, {"query": "Pinnacle"}, "second")["status"], "lookup_budget_exhausted")
                self.assertIn(returned["evidence_id"], {c.id for c in review.repacked.selected})

        review = SourceReview(self.retriever, replace(self.source, metadata={}), pack, limits,
                              lambda *args, **kwargs: None)
        self.assertEqual(review.call(CHARACTER, {}, "unknown")["status"], "speaker_unresolved")
        store.invalidate(approved.ref, "fixture superseded")
        self.assertEqual(self.retriever.lookup_memory(self.source, pack, TM, "Pinnacle")[1], [])
