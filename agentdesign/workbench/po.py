"""Native Naev Gettext import, occurrence binding and selective export."""

from collections import defaultdict
from codecs import BOM_UTF8
from dataclasses import dataclass
import gettext
from io import BytesIO
import json
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory

import polib

from localization_agent.schemas.memory import text_hash
from .job_store import JobError, encode

MAX_UPLOAD_BYTES = 16 * 1024 * 1024
PO_SCHEMA = """CREATE TABLE IF NOT EXISTS p3_uploads (
    id TEXT PRIMARY KEY, filename TEXT NOT NULL, content BLOB NOT NULL
)"""


def identity(entry):
    return (entry.msgctxt, entry.msgid, entry.msgid_plural or "")


def signature(entry):
    return (identity(entry), entry.msgstr, entry.msgstr_plural, entry.obsolete, entry.comment,
            entry.tcomment, entry.flags, entry.occurrences, entry.previous_msgctxt,
            entry.previous_msgid, entry.previous_msgid_plural)


def slots(entry):
    return tuple(sorted({0, *entry.msgstr_plural})) if entry.msgid_plural else (None,)


def read_po(content):
    try:
        text = content.decode("utf-8-sig")
        if "\n" not in text or not text.lstrip().startswith(("#", "msgid", "msgctxt")) or "\x00" in text:
            raise ValueError("expected PO content")
        po = polib.pofile(text, encoding="utf-8")
    except (UnicodeError, ValueError, OSError, IndexError) as error:
        raise JobError(422, "invalid_utf8_po") from error
    if not po or not po.metadata:
        raise JobError(422, "po_header_and_entries_required")
    language = po.metadata.get("Language", "").lower().replace("-", "_")
    if language not in {"zh", "zh_cn", "zh_hans", "zh_hans_cn"}:
        raise JobError(422, "simplified_chinese_po_required")
    charset = re.search(r"charset=([^;\s]+)", po.metadata.get("Content-Type", ""), re.I)
    if charset is None or charset[1].lower().replace("-", "") != "utf8":
        raise JobError(422, "utf8_po_header_required")
    if any(e.msgid_plural for e in po if not e.obsolete):
        form = po.metadata.get("Plural-Forms", "")
        if not re.fullmatch(r"\s*nplurals\s*=\s*1\s*;\s*plural\s*=\s*\(?\s*0\s*\)?\s*;?\s*", form):
            raise JobError(422, "simplified_chinese_plural_form_required")
    live = [(e.msgctxt, e.msgid) for e in po if not e.obsolete]
    if len(set(live)) != len(live):
        raise JobError(422, "duplicate_gettext_key")
    return po


class NativeCatalog:
    def __init__(self, db, provenance_path):
        self.keys = defaultdict(list)
        self.resources = {}
        if provenance_path is None:
            return
        sources = {}
        for row in db.execute("""SELECT id, version, payload FROM sources WHERE project='naev'
                                 ORDER BY version"""):
            data = json.loads(row["payload"])
            sources[row["id"]] = (row["version"], data["source"], data["metadata"].get("branch_id"))
        with provenance_path.open(encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                item = row["record_id"]
                if item not in sources:
                    continue
                version, text, branch = sources[item]
                key = (row["msgctxt"] or None, row["msgid"], row["msgid_plural"] or "")
                form = row["source_form"]
                expected = key[2] if form == "plural" else key[1]
                if text != expected:
                    continue
                location = {k: row["source"][k] for k in ("path", "line", "xpath") if k in row["source"]}
                self.keys[key].append(item)
                self.resources[item] = {
                    "occurrence_id": item, "label": text, "source_version": version,
                    "file": location.get("path"), "branch_id": branch, "location": location, "source_form": form,
                }

    def scope(self, po):
        return {item: self.resources[item] for entry in po if not entry.obsolete
                for item in self.keys.get(identity(entry), ())}

    def rows(self, po, query="", offset=0, limit=30):
        rows = []
        total = 0
        for entry in po:
            if entry.obsolete or (query and query.casefold() not in
                                  " ".join(str(value or "") for value in identity(entry)).casefold()):
                continue
            if offset <= total < offset + limit:
                key = identity(entry)
                rows.append({
                    "id": text_hash(encode(key)), "context": entry.msgctxt, "source": entry.msgid,
                    "source_plural": entry.msgid_plural or None,
                    "plural_slots": slots(entry),
                    "occurrences": [self.resources[item] for item in self.keys.get(key, ())],
                })
            total += 1
        return rows, total


@dataclass
class NativeExport:
    report: dict
    content: bytes


def patch_translation(lines, start, end, plural, text, clear_fuzzy):
    """Change only the translation field and fuzzy marker of this physical entry."""
    field = "msgstr[0]" if plural else "msgstr"
    position = next((i for i in range(start, end) if lines[i].strip().startswith(field + " ")
                     or lines[i].strip().startswith(field + "\t")), None)
    if position is None:
        raise JobError(422, "po_target_slot_missing")
    old_line = lines[position]
    line_end = old_line[len(old_line.rstrip("\r\n")):]
    indent = old_line[:len(old_line) - len(old_line.lstrip(" \t"))]
    lines[position] = f'{indent}{field} "{polib.escape(text)}"{line_end}'
    for i in range(position + 1, end):
        if not lines[i].lstrip().startswith('"'):
            break
        lines[i] = ""
    if clear_fuzzy:
        for i in range(start, position):
            if lines[i].lstrip().startswith("#,"):
                flags = [flag.strip() for flag in lines[i].lstrip()[2:].split(",")]
                if "fuzzy" in flags:
                    flags.remove("fuzzy")
                    ending = lines[i][len(lines[i].rstrip("\r\n")):]
                    lines[i] = "#, " + ", ".join(flags) + ending if flags else ""


def validate_rendered(content, approved):
    checks = {"parser": True}
    tool = shutil.which("msgfmt")
    if not tool:
        return {"ok": True, "level": "parser_only", "checks": checks}
    with TemporaryDirectory() as tmp:
        path = Path(tmp)
        (path / "catalog.po").write_bytes(content)
        try:
            result = subprocess.run([tool, "--check", "--output-file=catalog.mo", "catalog.po"],
                                    cwd=path, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as error:
            return {"ok": False, "level": "gettext", "checks": checks, "reason": type(error).__name__}
        checks["msgfmt"] = result.returncode == 0
        diagnostics = result.stderr.decode("utf-8", errors="replace")
        if not checks["msgfmt"]:
            return {"ok": False, "level": "gettext", "checks": checks, "diagnostics": diagnostics[:3000]}
        try:
            with (path / "catalog.mo").open("rb") as compiled:
                mo = gettext.GNUTranslations(BytesIO(compiled.read()))
            for key, text in approved.items():
                ctx, source, plural = key
                actual = (mo.npgettext(ctx, source, plural, 2) if ctx is not None else mo.ngettext(source, plural, 2)
                          ) if plural else mo.pgettext(ctx, source) if ctx is not None else mo.gettext(source)
                if actual != text:
                    return {"ok": False, "level": "gettext", "checks": checks, "reason": "mo_translation_mismatch"}
        except (OSError, UnicodeError, ValueError) as error:
            return {"ok": False, "level": "gettext", "checks": checks, "reason": type(error).__name__}
        checks["mo_load"] = True
    return {"ok": True, "level": "gettext", "checks": checks, "diagnostics": diagnostics[:3000]}


def render(raw, catalog, records, excluded, choices):
    """Report conflicts per native key before choosing an already-approved occurrence."""
    po = read_po(raw)
    lines = raw.decode("utf-8-sig").splitlines(keepends=True)
    translations = {row["occurrence_id"]: row for row in records}
    unavailable = {row["occurrence_id"]: row["reason"] for row in excluded}
    changed, conflicts, skipped, approved = [], [], [], {}
    consumed = set()
    for index, entry in enumerate(po):
        if entry.obsolete:
            continue
        key = identity(entry)
        key_id = text_hash(encode(key))
        linked = catalog.keys.get(key, ())
        versions = [translations[item] for item in linked if item in translations]
        if not versions:
            skipped.extend({"occurrence_id": item, "reason": unavailable[item]} for item in linked
                           if item in unavailable)
            continue
        skipped.extend({"occurrence_id": item, "reason": unavailable[item]} for item in linked
                       if item in unavailable)
        if any(item in unavailable for item in linked):
            skipped.append({"entry_id": key_id, "reason": "shared_key_has_unapproved_occurrence"})
            continue
        if len({version["target"] for version in versions}) > 1:
            wanted = choices.get(key_id)
            if wanted is None:
                conflicts.append({"entry_id": key_id, "context": entry.msgctxt, "source": entry.msgid,
                                  "source_plural": entry.msgid_plural or None, "versions": versions})
                continue
            selected = next((row for row in versions if row["occurrence_id"] == wanted), None)
            if selected is None:
                raise JobError(422, "po_conflict_choice_must_use_approved_version")
            consumed.add(key_id)
        else:
            selected = versions[0]
        text = selected["target"]
        previous = entry.msgstr_plural.get(0, "") if entry.msgid_plural else entry.msgstr
        fuzzy = "fuzzy" in entry.flags
        if previous != text or fuzzy:
            end = po[index + 1].linenum - 1 if index + 1 < len(po) else len(lines)
            patch_translation(lines, entry.linenum - 1, end, bool(entry.msgid_plural), text, fuzzy)
        if entry.msgid_plural:
            entry.msgstr_plural[0] = text
        else:
            entry.msgstr = text
        if fuzzy:
            entry.flags = [flag for flag in entry.flags if flag != "fuzzy"]
        approved[key] = text
        if previous != text or fuzzy:
            changed.append({"entry_id": key_id, "context": entry.msgctxt, "source": entry.msgid,
                            "source_plural": entry.msgid_plural or None, "slot": 0 if entry.msgid_plural else None,
                            "previous": previous, "target": text, "occurrence_id": selected["occurrence_id"],
                            "version": selected["version"], "fuzzy_cleared": fuzzy})
    if choices.keys() - consumed:
        raise JobError(422, "po_conflict_choice_does_not_match_current_conflict")
    content = (BOM_UTF8 if raw.startswith(BOM_UTF8) else b"") + "".join(lines).encode("utf-8")
    parsed = read_po(content)
    if (po.metadata != parsed.metadata or po.metadata_is_fuzzy != parsed.metadata_is_fuzzy
            or po.header != parsed.header or list(map(signature, po)) != list(map(signature, parsed))):
        raise JobError(422, "po_roundtrip_changed_entry")
    validation = validate_rendered(content, approved)
    report = {"validation": validation, "changes": changed, "applied": len(approved),
              "conflicts": conflicts, "exclusions": skipped, "total_entries": len(po),
              "download_ready": validation["ok"] and not conflicts and bool(approved)}
    return NativeExport(report, content)
