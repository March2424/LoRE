"""Restore actual P1 ContextPacks at the approval boundary."""

from localization_agent.schemas.graph import Candidate, CandidatePool, ContextPack, GraphRef
from localization_agent.schemas.memory import Evidence, MemoryRef, ReadView


def restore_candidate(row):
    return Candidate(**{
        **row, "memory_ref": MemoryRef(**row["memory_ref"]) if row["memory_ref"] else None,
        "sources": tuple(Evidence(**value) for value in row["sources"]),
        "graph_refs": tuple(GraphRef(**value) for value in row["graph_refs"]),
        "relations": tuple(row["relations"]),
    })


def restore_pack(row):
    pool = row["pool"]
    return ContextPack(**{
        **row, "selected": tuple(map(restore_candidate, row["selected"])),
        "pool": CandidatePool(**{
            **pool, "view": ReadView(**pool["view"]),
            "candidates": tuple(map(restore_candidate, pool["candidates"])),
            "conflicts": tuple(pool["conflicts"]), "unresolved": tuple(pool["unresolved"]),
            "binding_refs": tuple(GraphRef(**value) for value in pool["binding_refs"]),
        }),
    })
