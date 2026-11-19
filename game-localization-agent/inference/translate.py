"""Translate source resources from a completed graph with isolated run memory."""
import argparse
from collections import Counter
from contextlib import closing
from dataclasses import asdict, replace
import json
from pathlib import Path
import sqlite3

from localization_agent.memory import MemoryConfig, MemoryService, MemoryStore
from localization_agent.models.chat_completions import CallBudget, ChatCompletionsModel
from localization_agent.models.structured import parse_json_reply
from localization_agent.models.token_count import LocalTokenCounter
from localization_agent.retrieval import GraphConfig, GraphRetriever, GraphStore
from localization_agent.schemas.memory import Occurrence, TranslationCheck, text_hash
from localization_agent.selection.teacher import translation_issues
from localization_agent.workflow.game import GameSession


def run(graph_path, output, project, source_lang, target_lang, chat, selector=None,
        tokenizer=None, ids=None, budget=None):
    graph_path = graph_path.resolve(strict=True)
    output.mkdir(parents=True, exist_ok=False)
    store = MemoryStore(output / "run.sqlite")
    try:
        with closing(sqlite3.connect(graph_path.as_uri() + "?mode=ro", uri=True)) as source:
            source.backup(store.db)
        rows = store.db.execute(
            "SELECT s.payload FROM sources s WHERE s.project=? AND s.version=("
            "SELECT max(version) FROM sources WHERE project=s.project AND id=s.id)", (project,)
        )
        occurrences = [Occurrence(**json.loads(row["payload"])) for row in rows]
        occurrences = sorted((o for o in occurrences if (o.source_lang, o.target_lang) ==
                              (source_lang, target_lang)), key=lambda o: (o.document_id, o.position))
        if ids is not None:
            by_id = {o.occurrence_id: o for o in occurrences}
            occurrences = [by_id[key] for key in ids]
        if not occurrences:
            raise ValueError("No source records for this project and direction")

        graph = GraphStore(store)
        memory = MemoryService(store, MemoryConfig.from_file("configs/memory.json"), chat=chat)
        config = GraphConfig.from_file("configs/graph.json")
        if tokenizer is None:
            config = replace(config, max_soft_tokens=None)
        retriever = GraphRetriever(graph, memory, config, token_counter=tokenizer)
        session = GameSession(retriever)
        run_id = "submission-translation"
        store.create_snapshot("submission-knowledge", project)
        store.start_run(run_id, project, None, source_lang, target_lang, "submission-knowledge",
                        mode="game_resources")
        records = []
        with (output / "translations.jsonl").open("x", encoding="utf-8") as handle:
            for occurrence in occurrences:
                record = translate(occurrence, run_id, retriever, session, selector)
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                records.append(record)
        usage = budget.snapshot() if budget else None
        if usage and usage["unpriced_calls"]:
            usage["cost"] = None
        summary = {"statuses": dict(Counter(row["status"] for row in records)),
                   "visible_memory": dict(Counter(r.kind for r in store.visible(store.view(run_id)))),
                   "budget": usage}
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        return summary
    finally:
        store.close()


def translate(source, run_id, retriever, session, selector):
    memory = session.memory
    store = memory.store
    row = {"source_id": source.occurrence_id, "status": "failed", "hyp": "", "issues": []}
    try:
        pool = retriever.retrieve(source, store.view(run_id), "graph")
        if selector is None:
            pack = retriever.build_context(pool)
        else:
            observation = json.dumps(retriever.selector_observation(pool), ensure_ascii=False)
            prompt = Path("prompts/selection/context_selector.txt").read_text().format(observation=observation)
            reply = selector("teacher_select", prompt)
            store.log_call("teacher_select", prompt, asdict(reply))
            action = parse_json_reply(reply.text)
            if not isinstance(action, dict) or not isinstance(action.get("rationale"), str) or not action["rationale"].strip():
                raise ValueError("Selector must return a rationale and selected_ids")
            keys = action.get("selected_ids")
            if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
                raise ValueError("selected_ids must be an array of candidate IDs")
            pack = retriever.select_context(pool, tuple(keys))
            row["selection"] = action
        row.update(context=pack.manifest, candidate_ids=[c.id for c in pool.candidates])
        if pack.status != "ready":
            raise ValueError(f"Context pack is {pack.status}")
        target, _ = memory._call("translate_game", "translate_game.txt", source=source.source,
                                 source_lang=source.source_lang, target_lang=source.target_lang,
                                 contexts=pack.model_context,
                                 source_context=pack.manifest["current_source_context"])
        row["hyp"] = target
        row["issues"] = translation_issues(source, target, pack, session)
        if row["issues"]:
            return row
        check = TranslationCheck(source.occurrence_id, source.source_version, text_hash(target),
                                 pack.context_hash, "program:submission-unreviewed", False)
        saved = retriever.commit(source, target, check, pack, allow_unreviewed=True)
        row.update(status="recorded_unreviewed", memory_ref=asdict(saved.ref))
    except (ValueError, RuntimeError) as error:
        row["issues"].append(f"{type(error).__name__}: {error}")
        return row

    row["memory_updates"] = {}
    for kind in ("term", "summary", "style"):
        try:
            drafts = (memory.extract_terms(saved.ref) if kind == "term" else
                      retriever.update_memory(source, store.view(run_id), "graph", kind=kind))
            row["memory_updates"][kind] = [
                asdict(store.approve(draft.ref, "program:submission-unreviewed",
                                    verification="unreviewed").ref) for draft in drafts
            ]
        except (ValueError, RuntimeError) as error:
            row["issues"].append(f"{kind}: {type(error).__name__}: {error}")
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--source-lang", default="en")
    parser.add_argument("--target-lang", default="zh")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path)
    parser.add_argument("--ids", type=Path, help="Optional source ID per line, in translation order")
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    budget = CallBudget(**config["budget"])
    translation = ChatCompletionsModel(**config["model"], budget=budget)
    update = ChatCompletionsModel(**config.get("memory_model", config["model"]), budget=budget)
    selector = ChatCompletionsModel(**config["selector_model"], budget=budget) if "selector_model" in config else None

    def chat(task, prompt):
        return (translation if task == "translate_game" else update)(task, prompt)

    tokenizer = LocalTokenCounter(args.tokenizer) if args.tokenizer else None
    ids = args.ids.read_text().splitlines() if args.ids else None
    summary = run(args.graph, args.output, args.project, args.source_lang, args.target_lang, chat,
                  selector=selector, tokenizer=tokenizer, ids=ids, budget=budget)
    print(json.dumps(summary, ensure_ascii=False))
    return int(summary["statuses"].get("failed", 0) != 0)


if __name__ == "__main__":
    raise SystemExit(main())
