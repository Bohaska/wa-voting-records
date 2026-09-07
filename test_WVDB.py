import json
import random
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

import WVDB


ROOT = Path(__file__).parent


def with_crc(body: bytes) -> bytes:
    return body + struct.pack("<I", __import__("zlib").crc32(body) & 0xFFFFFFFF)


def split_blocks(data: bytes):
    crc_offset = len(data) - 4
    offset = 6
    rows, offset = WVDB._read_uvarint(data, offset, crc_offset)
    columns, offset = WVDB._read_uvarint(data, offset, crc_offset)
    block_count, offset = WVDB._read_uvarint(data, offset, crc_offset)
    blocks = []
    for _ in range(block_count):
        block_type = data[offset]
        flags = data[offset + 1]
        offset += 2
        length, offset = WVDB._read_uvarint(data, offset, crc_offset)
        payload = data[offset : offset + length]
        offset += length
        blocks.append((block_type, flags, payload))
    assert offset == crc_offset
    return rows, columns, blocks


def rebuild(rows, columns, blocks):
    fixed = b"WVDB\x01\x00" + WVDB._encode_uvarint(rows) + WVDB._encode_uvarint(columns)
    fixed += WVDB._encode_uvarint(len(blocks))
    body = bytearray(fixed)
    for block_type, flags, payload in blocks:
        body.extend((block_type, flags))
        body.extend(WVDB._encode_uvarint(len(payload)))
        body.extend(payload)
    return with_crc(bytes(body))


class WVDBTests(unittest.TestCase):
    def assert_round_trip(self, table):
        encoded = WVDB.encode_table(table)
        decoded = WVDB.decode(encoded)
        self.assertEqual(decoded, table)
        self.assertEqual(decoded.columns, table.columns)
        self.assertEqual(decoded.ids, table.ids)
        for row in range(table.row_count):
            for column in range(table.column_count):
                expected = table.get_vote(row, column)
                self.assertEqual(decoded.has_vote(row, column), expected is not None)
                self.assertEqual(decoded.get_vote(row, column), expected)
        return encoded

    def test_empty_and_single_cell_tables(self):
        cases = [
            WVDB.table_from_matrix([], [], []),
            WVDB.table_from_matrix(["a", "b"], [], []),
            WVDB.table_from_matrix([], ["a", "b"], [[], []]),
            WVDB.table_from_matrix(["a"], ["a"], [[""]]),
            WVDB.table_from_matrix(["a"], ["a"], [["0"]]),
            WVDB.table_from_matrix(["a"], ["a"], [["1"]]),
        ]
        for table in cases:
            with self.subTest(table=table):
                self.assert_round_trip(table)

    def test_dimension_boundaries(self):
        for columns in (1, 7, 8, 9, 101, 127, 128, 129, 130):
            names = [f"column-{index:03d}" for index in range(columns)]
            ids = ["nation-000"]
            matrix = [["1" if index % 3 == 0 else "" for index in range(columns)]]
            with self.subTest(columns=columns):
                self.assert_round_trip(WVDB.table_from_matrix(names, ids, matrix))
        for rows in (1, 127, 128, 129):
            ids = [f"nation-{index:03d}" for index in range(rows)]
            matrix = [["0"] if index % 2 else [""] for index in range(rows)]
            with self.subTest(rows=rows):
                self.assert_round_trip(WVDB.table_from_matrix(["column"], ids, matrix))

    def test_randomized_round_trips(self):
        randomizer = random.Random(0x5A17)
        for case in range(350):
            rows = randomizer.randrange(0, 18)
            columns = randomizer.randrange(0, 18)
            names = [f"列-{index}-é" for index in range(columns)]
            ids = [f"国-{index:03d}" for index in range(rows)]
            matrix = [
                [randomizer.choice(("", "", "", "0", "1")) for _ in range(columns)]
                for _ in range(rows)
            ]
            with self.subTest(case=case, rows=rows, columns=columns):
                self.assert_round_trip(WVDB.table_from_matrix(names, ids, matrix))

    def test_current_dataset_logical_round_trip(self):
        source = WVDB.read_csv(ROOT / "votes.csv")
        decoded = WVDB.decode(WVDB.encode_table(source))
        self.assertEqual(decoded.columns, source.columns)
        self.assertEqual(decoded.ids, source.ids)
        self.assertEqual(decoded.participation, source.participation)
        self.assertEqual(decoded.votes, source.votes)

    def test_csv_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            path.write_text("nation_id,c\na,2\n", encoding="utf-8")
            with self.assertRaises(WVDB.WVDBError):
                WVDB.read_csv(path)
            path.write_text("nation_id,c\nb,1\na,0\n", encoding="utf-8")
            with self.assertRaises(WVDB.WVDBError):
                WVDB.read_csv(path)
            path.write_text("nation_id,c\na,1,0\n", encoding="utf-8")
            with self.assertRaises(WVDB.WVDBError):
                WVDB.read_csv(path)

    def test_determinism_and_atomic_cli(self):
        table = WVDB.table_from_matrix(["a", "b"], ["a", "b"], [["0", ""], ["1", "1"]])
        first = WVDB.encode_table(table)
        second = WVDB.encode_table(table)
        self.assertEqual(first, second)
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "input.csv"
            source.write_text("nation_id,a,b\na,0,\nb,1,1\n", encoding="utf-8")
            raw = directory / "out.wvdb"
            decoded = directory / "decoded.csv"
            self.assertEqual(WVDB.main(["encode", str(source), str(raw)]), 0)
            self.assertEqual(WVDB.main(["decode", str(raw), str(decoded)]), 0)
            self.assertEqual(WVDB.read_csv(decoded), table)

    def test_corruption_and_framing_rejections(self):
        valid = WVDB.encode_table(WVDB.table_from_matrix(["a"], ["a"], [["1"]]))
        with self.assertRaises(WVDB.WVDBError):
            WVDB.decode(b"XVDB" + valid[4:])
        with self.assertRaises(WVDB.WVDBError):
            WVDB.decode(valid[:-1])
        with self.assertRaises(WVDB.WVDBError):
            WVDB.decode(valid + b"trailing")
        changed_version = bytearray(valid)
        changed_version[4] = 2
        with self.assertRaises(WVDB.WVDBError):
            WVDB.decode(changed_version)

        rows, columns, blocks = split_blocks(valid)
        with self.assertRaises(WVDB.WVDBError):
            WVDB.decode(rebuild(rows, columns, blocks[:-1]))
        with self.assertRaises(WVDB.WVDBError):
            WVDB.decode(rebuild(rows, columns, blocks + [blocks[0]]))
        unknown = blocks + [(0x7F, 0, b"future")]
        self.assertEqual(WVDB.decode(rebuild(rows, columns, unknown)).get_vote(0, 0), 1)
        with self.assertRaises(WVDB.WVDBError):
            WVDB.decode(rebuild(rows, columns, blocks + [(0x7F, 1, b"future")]))
        bad_flags = blocks.copy()
        bad_flags[0] = (bad_flags[0][0], 2, bad_flags[0][2])
        with self.assertRaises(WVDB.WVDBError):
            WVDB.decode(rebuild(rows, columns, bad_flags))

        invalid_utf8 = blocks.copy()
        payload = bytearray(invalid_utf8[0][2])
        payload[-1] = 0xFF
        invalid_utf8[0] = (invalid_utf8[0][0], invalid_utf8[0][1], bytes(payload))
        with self.assertRaises(WVDB.WVDBError):
            WVDB.decode(rebuild(rows, columns, invalid_utf8))

        padded = WVDB.table_from_matrix(["a"], ["a"], [["1"]])
        padded_bytes = WVDB.encode_table(padded)
        p_rows, p_columns, p_blocks = split_blocks(padded_bytes)
        p_blocks[2] = (3, 1, b"\x81")
        with self.assertRaises(WVDB.WVDBError):
            WVDB.decode(rebuild(p_rows, p_columns, p_blocks))

    def test_malformed_varint(self):
        valid = WVDB.encode_table(WVDB.table_from_matrix([], [], []))
        body = valid[:-4]
        malformed = body[:6] + b"\x80\x00" + body[7:]
        with self.assertRaises(WVDB.WVDBError):
            WVDB.decode(with_crc(malformed))

    @unittest.skipUnless(__import__("shutil").which("node"), "Node.js is unavailable")
    def test_javascript_decoder(self):
        table = WVDB.table_from_matrix(
            ["α", "β", "γ"],
            ["a", "b", "c"],
            [["", "0", "1"], ["1", "", "0"], ["0", "1", ""]],
        )
        encoded = WVDB.encode_table(table)
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "table.wvdb"
            binary.write_bytes(encoded)
            script = """
const fs = require('fs');
const WVDB = require(process.argv[1]);
const table = WVDB.decode(fs.readFileSync(process.argv[2]));
process.stdout.write(JSON.stringify({
  rows: table.rowCount,
  columns: table.columnCount,
  values: [table.getVote(0, 0), table.getVote(0, 1), table.getVote(0, 2), table.getVote(1, 0)]
}));
"""
            result = subprocess.run(
                ["node", "-e", script, str(ROOT / "WVDB.js"), str(binary)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(result.stdout), {"rows": 3, "columns": 3, "values": [None, 0, 1, 1]})


if __name__ == "__main__":
    unittest.main()
