import json
import tempfile
import unittest
from pathlib import Path

from agentic.ledger import GENESIS, Ledger, LedgerError, compute_hash
from helpers import TimedTestCase

TYPES = ["mission_started", "baseline", "iteration_worker", "check_result", "check_result", "mission_finished"]


class LedgerTests(TimedTestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "ledger.jsonl"
        self.ledger = Ledger(self.path)
        for i, record_type in enumerate(TYPES):
            self.ledger.append(record_type, {"n": i, "text": "한글 ok"})

    def lines(self) -> list[str]:
        return self.path.read_text(encoding="utf-8").splitlines()

    def write_lines(self, lines: list[str]) -> None:
        self.path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")

    def test_records_chain_correctly(self):
        records = self.ledger.read()
        self.assertEqual([r["seq"] for r in records], list(range(1, len(TYPES) + 1)))
        self.assertEqual(records[0]["prev"], GENESIS)
        for before, after in zip(records, records[1:]):
            self.assertEqual(after["prev"], before["hash"])
        for rec in records:
            self.assertEqual(rec["hash"], compute_hash(rec))
        self.assertTrue(self.ledger.verify().ok)

    def test_chain_continues_in_a_new_instance(self):
        Ledger(self.path).append("stopped_time", {})
        self.assertEqual(len(Ledger(self.path).read()), len(TYPES) + 1)

    def test_editing_any_record_is_detected_at_that_record(self):
        for i in range(len(TYPES)):
            with self.subTest(record=i):
                lines = self.lines()
                rec = json.loads(lines[i])
                rec["data"]["n"] = 999
                edited = lines[:i] + [json.dumps(rec, ensure_ascii=False)] + lines[i + 1:]
                self.write_lines(edited)
                result = Ledger(self.path).verify()
                self.assertFalse(result.ok)
                self.assertEqual(result.bad_seq, i + 1)
                with self.assertRaises(LedgerError):
                    Ledger(self.path).read()
                self.write_lines(lines)

    def test_editing_and_rehashing_a_record_breaks_the_next_link(self):
        lines = self.lines()
        rec = json.loads(lines[2])
        rec["data"]["n"] = 999
        rec["hash"] = compute_hash(rec)
        lines[2] = json.dumps(rec, ensure_ascii=False)
        self.write_lines(lines)
        self.assertEqual(Ledger(self.path).verify().bad_seq, 4)

    def test_deleting_any_record_but_the_last_is_detected(self):
        # A chain cannot show that records were cut off the very end; see DECISIONS.md.
        for i in range(len(TYPES) - 1):
            with self.subTest(record=i):
                lines = self.lines()
                self.write_lines(lines[:i] + lines[i + 1:])
                result = Ledger(self.path).verify()
                self.assertFalse(result.ok)
                self.assertEqual(result.bad_seq, i + 1)
                self.write_lines(lines)

    def test_reordering_records_is_detected(self):
        for i in range(len(TYPES) - 1):
            with self.subTest(swap=i):
                lines = self.lines()
                swapped = list(lines)
                swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
                self.write_lines(swapped)
                self.assertEqual(Ledger(self.path).verify().bad_seq, i + 1)
                self.write_lines(lines)

    def test_torn_last_line_is_reported(self):
        with self.path.open("ab") as handle:
            handle.write(b'{"seq": 7, "ts"')
        result = Ledger(self.path).verify()
        self.assertFalse(result.ok)
        self.assertEqual(result.count, 0)

    def test_append_refuses_to_extend_a_broken_ledger(self):
        lines = self.lines()
        self.write_lines(lines[:1] + lines[2:])
        with self.assertRaises(LedgerError):
            Ledger(self.path).append("stopped_time", {})


if __name__ == "__main__":
    unittest.main()
