"""Source-only import and native coordinates for project files."""

import csv
from io import BytesIO, StringIO
from pathlib import Path
from xml.etree.ElementTree import ParseError

import polib

from .job_store import JobError
from .po import identity, validate_rendered

META = {"text_type", "native_context", "document", "scene", "block", "branch_id", "phase_id",
        "speaker_id", "addressee_id", "topic_id", "requirements"}
MAX_SOURCE_ROWS = 100_000


def table_rows(content, suffix, options):
    if suffix == ".csv":
        return csv.reader(StringIO(content.decode("utf-8-sig"), newline="")), None
    from openpyxl import load_workbook
    from openpyxl.utils.exceptions import InvalidFileException
    try:
        book = load_workbook(BytesIO(content), read_only=True, data_only=False)
    except (InvalidFileException, ParseError) as error:
        raise JobError(422, f"invalid_import:{type(error).__name__}") from None
    try:
        sheet = book[options["sheet"]] if options.get("sheet") else book.worksheets[0]
    except KeyError:
        book.close()
        raise JobError(422, "worksheet_not_found") from None
    return sheet.values, book


def parse_tables(filename, content, options):
    suffix = Path(filename).suffix.lower()
    rows, book = table_rows(content, suffix, options)
    try:
        rows = iter(rows)
        header = [str(value).strip() if value is not None else "" for value in next(rows, ())]
        used = [value for value in header if value]
        if len(used) != len(set(used)):
            raise JobError(422, "duplicate_column_header")
        mapping = {"id": options.get("id_column", "id"), "source": options.get("source_column", "source_text"),
                   **{key: key for key in META if key in header}, **options.get("columns", {})}
        if "context" in header and "native_context" not in options.get("columns", {}):
            mapping["native_context"] = "context"
        target = options.get("target_column")
        if target:
            if target in mapping.values():
                raise JobError(422, "target_column_must_be_separate")
            mapping["target"] = target
        if any(name not in header for field, name in mapping.items() if field != "target"):
            raise JobError(422, "missing_import_columns")
        indices = {field: header.index(name) for field, name in mapping.items() if name in header}
        records, skipped, keys = [], [], set()
        sheet = options.get("sheet") or (book.sheetnames[0] if book else None)
        for number, values in enumerate(rows, 2):
            if number > MAX_SOURCE_ROWS + 1:
                raise JobError(413, "too_many_source_rows")
            values = list(values)

            def cell(field):
                idx = indices.get(field)
                return values[idx] if idx is not None and idx < len(values) else None

            key, source = cell("id"), cell("source")
            if not any(value is not None and value != "" for value in values):
                continue
            if key is None or not str(key).strip():
                raise JobError(422, f"missing_source_id_at_row_{number}")
            key = str(key).strip()
            if key in keys:
                raise JobError(422, f"duplicate_source_id_at_row_{number}")
            keys.add(key)
            if not isinstance(source, str) or not source.strip():
                skipped.append({"row": number, "key": key, "reason": "empty_or_nontext_source"})
                continue
            if book and source.startswith("="):
                raise JobError(422, f"formula_source_at_row_{number}")
            location = {"path": filename, "row": number, "source_column": indices["source"] + 1}
            if sheet:
                location["sheet"] = sheet
            metadata = {field: str(cell(field)).strip() for field in META if cell(field) is not None
                        and str(cell(field)).strip()}
            records.append({"key": key, "source": source, "metadata": metadata, "location": location})
        return records, skipped, mapping.get("target") or "translation"
    finally:
        if book:
            book.close()


def project_po(content):
    text = content.decode("utf-8-sig")
    if "\n" not in text or "\x00" in text or not text.lstrip().startswith(("#", "msgid", "msgctxt")):
        raise JobError(422, "invalid_po_content")
    po = polib.pofile(text, encoding="utf-8")
    if not po or not po.metadata or len({(e.msgctxt, e.msgid) for e in po if not e.obsolete}) != len(
            [e for e in po if not e.obsolete]):
        raise JobError(422, "invalid_or_duplicate_po_entries")
    return po


def parse_po(filename, content):
    from .job_store import encode
    records, skipped = [], []
    for entry in project_po(content):
        if entry.obsolete:
            continue
        context = entry.msgctxt or ""
        key = encode([context, entry.msgid])
        if not entry.msgid.strip():
            skipped.append({"line": entry.linenum, "reason": "empty_source"})
            continue
        # A plural pair belongs to one key but each source form has its own occurrence.
        for form, source in (("singular", entry.msgid), ("plural", entry.msgid_plural)):
            if not source:
                continue
            records.append({"key": encode([key, form]), "source": source,
                            "metadata": {"native_context": context} if context else {},
                            "location": {"path": filename, "line": entry.linenum, "msgctxt": context,
                                         "msgid": entry.msgid, "form": form, "references": entry.occurrences}})
    return records, skipped, None


def parse(filename, content, options):
    try:
        if filename.lower().endswith(".po"):
            return parse_po(filename, content)
        return parse_tables(filename, content, options)
    except (UnicodeError, csv.Error, OSError, ValueError, KeyError) as error:
        if isinstance(error, JobError):
            raise
        raise JobError(422, f"invalid_import:{type(error).__name__}") from None


def render_table(raw, filename, rows, targets, target_column):
    if filename.lower().endswith(".csv"):
        original = list(csv.reader(StringIO(raw.decode("utf-8-sig"), newline="")))
        header = original[0]
        names = [value.strip() for value in header]
        if target_column not in names:
            column = len(header)
            header.append(target_column)
            for cells in original[1:]:
                if cells:
                    cells.extend([""] * max(0, column - len(cells)))
                    cells.insert(column, "")
        else:
            column = names.index(target_column)
        for row in rows:
            location = row["location"]
            if row["occurrence_id"] in targets:
                cells = original[location["row"] - 1]
                cells.extend([""] * max(0, column + 1 - len(cells)))
                cells[column] = targets[row["occurrence_id"]]
        output = StringIO(newline="")
        csv.writer(output).writerows(original)
        return output.getvalue().encode("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8")
    from openpyxl import load_workbook
    book = load_workbook(BytesIO(raw))
    try:
        sheet = book[rows[0]["location"]["sheet"]]
        names = [str(cell.value).strip() if cell.value is not None else "" for cell in sheet[1]]
        column = names.index(target_column) + 1 if target_column in names else sheet.max_column + 1
        sheet.cell(1, column, target_column)
        for row in rows:
            item = row["occurrence_id"]
            if item in targets:
                cell = sheet.cell(row["location"]["row"], column, targets[item])
                cell.data_type = "s"
        output = BytesIO()
        book.save(output)
        return output.getvalue()
    finally:
        book.close()


def po_source_forms(rows):
    mapped = {}
    for row in rows:
        location = row["location"]
        mapped.setdefault((location["msgctxt"], location["msgid"]), {})[location["form"]] = row["occurrence_id"]
    return mapped


def po_writeback_exclusions(mapped, targets, target_lang):
    skipped = {}
    for forms in mapped.values():
        if any(item not in targets for item in forms.values()):
            skipped.update(dict.fromkeys((item for item in forms.values() if item in targets), "po_plural_translation_incomplete"))
        elif "plural" in forms and target_lang == "zh" and targets[forms["singular"]] != targets[forms["plural"]]:
            skipped.update(dict.fromkeys(forms.values(), "po_plural_translations_conflict"))
    return skipped


def render_project_po(raw, rows, targets, target_lang):
    po = project_po(raw)
    po.metadata["Language"] = "zh_Hans" if target_lang == "zh" else "en"
    po.metadata["Plural-Forms"] = "nplurals=1; plural=0;" if target_lang == "zh" else "nplurals=2; plural=(n != 1);"
    mapped = po_source_forms(rows)
    skipped, approved = po_writeback_exclusions(mapped, targets, target_lang), {}
    for entry in po:
        forms = mapped.get((entry.msgctxt or "", entry.msgid))
        if entry.obsolete or not forms or any(item not in targets or item in skipped for item in forms.values()):
            continue
        singular = targets[forms["singular"]]
        if not entry.msgid_plural:
            entry.msgstr = singular
            approved[identity(entry)] = singular
        elif target_lang == "en":
            entry.msgstr_plural = {0: singular, 1: targets[forms["plural"]]}
            approved[identity(entry)] = entry.msgstr_plural[1]
        else:
            entry.msgstr_plural = {0: singular}
            approved[identity(entry)] = singular
        entry.flags = [flag for flag in entry.flags if flag != "fuzzy"]
    content = str(po).encode("utf-8")
    project_po(content)
    return content, skipped, validate_rendered(content, approved)
