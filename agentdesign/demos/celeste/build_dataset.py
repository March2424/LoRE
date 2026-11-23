"""Build a local LoRE benchmark from a Steam installation of Celeste."""

import argparse
from collections import Counter
import csv
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re


DEFAULT_GAME = Path(r"D:\Steam\steamapps\common\Celeste")
DEFAULT_OUTPUT = Path(__file__).resolve().parents[2] / "runs" / "celeste-demo" / "dataset"
CSV_NAME = "celeste-en-zh.csv"
COLUMNS = ("id", "source_text", "text_type", "context")
INDEX_NAME = "record-index.csv"
INDEX_COLUMNS = ("id", "text_type", "source_line", "reference_line")
KEY = re.compile(r"^[ \t]*([A-Z][A-Z0-9_]*)[ \t]*=[ \t]*(.*)$")
PORTRAIT = re.compile(r"^\[([^\]\s]+)(?: [^\]]*)?\]$", re.M)
CONTROL = re.compile(r"\{[^{}\n]*\}")
CHAPTER = re.compile(r"^CH(\d+)_")
LANGUAGE_FIELDS = {"LANGUAGE", "ICON", "ORDER", "FONT", "SPLIT_REGEX", "COMMAS", "PERIODS", "BEGIN"}
CONTROL_FRAGMENTS = {"THOUGHT", "ENDTHOUGHT", "ST_BAD", "END_BAD"}


@dataclass(frozen=True)
class Entry:
    key: str
    text: str
    line: int
    section: str
    note: str


def parse_dialog(path, *, allow_overrides=False):
    entries = {}
    overrides = {}
    key, section, notes, value = None, "", [], []
    line_number, entry_section, entry_note = 0, "", ""

    def finish():
        if key is None:
            return
        if old := entries.get(key):
            if not allow_overrides:
                raise ValueError(f"{path.name}:{line_number}: duplicate key {key}")
            overrides.setdefault(key, [old.line]).append(line_number)
        entries[key] = Entry(key, "\n".join(value).strip("\n"), line_number, entry_section, entry_note)

    for number, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if match := KEY.fullmatch(raw):
            finish()
            key, text = match.groups()
            line_number, entry_section, entry_note = number, section, " ".join(notes)
            value, notes = [text.rstrip(" \t")], []
        elif raw.startswith("#"):
            finish()
            key, section, notes = None, raw.lstrip("# *").rstrip(" *"), []
        elif raw.lstrip("\t ").startswith("#"):
            notes.append(raw.strip("\t ").lstrip("# "))
        elif key is not None:
            value.append(raw.strip("\t "))
    finish()
    return entries, overrides


def kind(entry):
    key, section = entry.key, entry.section
    if key.startswith("POSTCARD_"):
        return "tutorial"
    if key.startswith("WAVEDASH_") or section == "Tutorial":
        return "tutorial"
    if key.startswith("POEM_"):
        return "collectible_text"
    if key.startswith("CH4_GONDOLA_FEATHER_"):
        return "narrative"
    if CHAPTER.match(key):
        return "dialogue" if PORTRAIT.search(entry.text) else "narrative"
    if section == "Chapter Titles" or re.fullmatch(r"AREA_\d+", key):
        return "chapter_title"
    if section in {"Checkpoints", "B-Side Checkpoints"}:
        return "checkpoint_name"
    if key in {"RESORT", "MOUNTAIN", "CELESTE_MOUNTAIN"}:
        return "location_name"
    if section == "Names" or key.startswith("THEO_SISTER_"):
        return "character_name"
    if section == "Intro Vignette" or key.startswith("END_"):
        return "narrative"
    if section == "Credits":
        return "credits"
    return "ui"


def native_context(entry):
    context = []
    if entry.section and not entry.section.startswith("="):
        context.append(f"section={entry.section}")
    if chapter := CHAPTER.match(entry.key):
        context.append(f"chapter={chapter[1]}")
    if speakers := list(dict.fromkeys(PORTRAIT.findall(entry.text))):
        context.append("portrait_speakers=" + ", ".join(speakers))
    if entry.note:
        context.append(f"native_note={entry.note}")
    return "; ".join(context)


def text_record(entry):
    if entry.key in LANGUAGE_FIELDS | CONTROL_FRAGMENTS or not entry.text:
        return False
    # Some name templates have only a save-data expression and formatting.
    # Keep other token-only entries such as buttons and cross-key labels.
    return not ("{savedata " in entry.text and not CONTROL.sub("", entry.text).strip())


def build(game, output):
    directory = game / "Content" / "Dialog"
    english_path, chinese_path = (directory / name for name in ("English.txt", "Simplified Chinese.txt"))
    english, _ = parse_dialog(english_path)
    chinese, overrides = parse_dialog(chinese_path, allow_overrides=True)
    selected = [entry for entry in english.values() if text_record(entry)]
    if len(selected) < 500:
        raise ValueError(f"Only {len(selected)} source records. Expected at least 500.")
    output.mkdir(parents=True, exist_ok=True)
    with (output / CSV_NAME).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\r\n")
        writer.writerow(COLUMNS)
        for entry in selected:
            writer.writerow((entry.key, entry.text, kind(entry), native_context(entry)))
    with (output / INDEX_NAME).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(INDEX_COLUMNS)
        for entry in selected:
            target = chinese.get(entry.key)
            writer.writerow((entry.key, kind(entry), entry.line, target.line if target else None))
    with (output / "references.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for entry in selected:
            target = chinese.get(entry.key)
            stream.write(json.dumps({
                "id": entry.key, "reference": target.text if target and target.text else None,
                "source_line": entry.line, "reference_line": target.line if target else None,
            }, ensure_ascii=False) + "\n")
    steam_manifest = game.parent.parent / "appmanifest_504230.acf"
    steam = steam_manifest.read_text(encoding="utf-8-sig") if steam_manifest.is_file() else ""
    build_id = re.search(r'"buildid"\s+"(\d+)"', steam)
    reference_count = sum(bool(chinese.get(entry.key) and chinese[entry.key].text) for entry in selected)
    manifest = {
        "game": "Celeste", "source_lang": "en", "target_lang": "zh-Hans",
        "steam_app_id": 504230, "steam_build_id": build_id[1] if build_id else None,
        "files": {path.name: {"path": path.relative_to(game).as_posix(),
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                  for path in (english_path, chinese_path)},
        "records": len(selected), "official_references": reference_count,
        "source_keys": len(english), "reference_keys": len(chinese), "reference_overrides": overrides,
        "types": dict(sorted(Counter(kind(entry) for entry in selected).items())),
        "excluded_source_keys": [entry.key for entry in english.values() if not text_record(entry)],
        "source_csv_sha256": hashlib.sha256((output / CSV_NAME).read_bytes()).hexdigest(),
        "record_index_sha256": hashlib.sha256((output / INDEX_NAME).read_bytes()).hexdigest(),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game", type=Path, default=DEFAULT_GAME)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    print(json.dumps(build(args.game, args.output), ensure_ascii=False, indent=2))
