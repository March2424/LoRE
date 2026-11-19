from types import SimpleNamespace
import unittest

import polib

from run_api import create_app  # Initializes the research package path.
from localization_agent.schemas.memory import text_hash
from workbench.job_store import JobError, encode
from workbench.po import identity, read_po, render, signature


class NativePoTests(unittest.TestCase):
    def fixture(self):
        po = polib.POFile()
        po.metadata = {
            "Project-Id-Version": "Naev P3 fixture", "PO-Revision-Date": "2026-11-18 00:00+0000",
            "Last-Translator": "Editor <editor@example.test>", "Language-Team": "Chinese",
            "Language": "zh_Hans", "MIME-Version": "1.0", "Content-Type": "text/plain; charset=UTF-8",
            "Content-Transfer-Encoding": "8bit", "Plural-Forms": "nplurals=1; plural=0;",
        }
        item = polib.POEntry(msgctxt="inventory", msgid="%d ship\n", msgid_plural="%d ships\n",
                             msgstr_plural={0: "%d 艘旧飞船\n"}, flags=["c-format", "fuzzy"],
                             occurrences=[("ships.lua", "5")], comment="Keep this comment intact.")
        untouched = polib.POEntry(msgctxt="score", msgid="%d ship\n", msgid_plural="%d ships\n",
                                  msgstr_plural={0: "%d 艘得分飞船\n"}, flags=["c-format"])
        obsolete = polib.POEntry(msgid="Former", msgstr="旧项", obsolete=True)
        po.extend([item, untouched, obsolete])
        raw = str(po).encode("utf-8").replace(b"\n", b"\r\n")
        return raw, identity(item)

    def test_plural_shared_identity_keeps_conflict_and_other_bytes(self):
        raw, key = self.fixture()
        native = SimpleNamespace(keys={key: ["singular-origin", "plural-origin"]})
        releases = [
            {"occurrence_id": "singular-origin", "target": "%d 艘飞船\n", "version": 1},
            {"occurrence_id": "plural-origin", "target": "%d 艘舰船\n", "version": 2},
        ]
        result = render(raw, native, releases, [], {})
        self.assertFalse(result.report["download_ready"])
        self.assertEqual(result.content, raw)
        conflict, = result.report["conflicts"]
        self.assertEqual(conflict["entry_id"], text_hash(encode(key)))
        with self.assertRaises(JobError):
            render(raw, native, releases, [], {conflict["entry_id"]: "unapproved"})
        result = render(raw, native, releases, [], {conflict["entry_id"]: "plural-origin"})
        self.assertTrue(result.report["download_ready"], result.report)
        before, after = read_po(raw), read_po(result.content)
        self.assertEqual(after[0].msgstr_plural, {0: "%d 艘舰船\n"})
        self.assertEqual(after[0].flags, ["c-format"])
        self.assertEqual(after[0].occurrences, before[0].occurrences)
        self.assertEqual(list(map(signature, before[1:])), list(map(signature, after[1:])))
        boundary = b'msgctxt "score"\r\n'
        self.assertEqual(result.content.split(boundary)[1], raw.split(boundary)[1])
        self.assertEqual(result.report["changes"][0]["slot"], 0)
        self.assertEqual(result.report["changes"][0]["version"], 2)

    def test_partial_delivery_leaves_shared_key_unmodified(self):
        raw, key = self.fixture()
        native = SimpleNamespace(keys={key: ["good", "unreviewed"]})
        result = render(raw, native, [{"occurrence_id": "good", "target": "%d 艘飞船\n", "version": 1}],
                        [{"occurrence_id": "unreviewed", "reason": "closed_by_editor"}], {})
        self.assertFalse(result.report["download_ready"])
        self.assertEqual(result.content, raw)
        self.assertEqual({item["reason"] for item in result.report["exclusions"]},
                         {"closed_by_editor", "shared_key_has_unapproved_occurrence"})
