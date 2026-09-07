#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import os
import struct
import sys
import tempfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence


MAGIC = b"WVDB"
VERSION = 1
FLAGS = 0
BLOCK_COLUMNS = 0x01
BLOCK_IDS = 0x02
BLOCK_PARTICIPATION = 0x03
BLOCK_VOTES = 0x04
REQUIRED_BLOCKS = (BLOCK_COLUMNS, BLOCK_IDS, BLOCK_PARTICIPATION, BLOCK_VOTES)
RANK_STRIDE_BITS = 512

MAX_ROWS = 10_000_000
MAX_COLUMNS = 1_000_000
MAX_CELLS = 100_000_000
MAX_STRING_BYTES = 64 * 1024 * 1024
MAX_BLOCKS = 1_000_000
MAX_BLOCK_PAYLOAD = 256 * 1024 * 1024
MAX_WVDB_BYTES = 256 * 1024 * 1024
U64_MAX = (1 << 64) - 1


class WVDBError(ValueError):
    pass

@dataclass(frozen=True)
class Table:
    columns: tuple[str, ...]
    ids: tuple[str, ...]
    participation: bytes
    votes: bytes
    _rank_index: tuple[int, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", tuple(self.columns))
        object.__setattr__(self, "ids", tuple(self.ids))
        object.__setattr__(self, "participation", bytes(self.participation))
        object.__setattr__(self, "votes", bytes(self.votes))
        _validate_table(self.columns, self.ids, self.participation, self.votes)
        object.__setattr__(self, "_rank_index", _build_rank_index(self.participation))

    @property
    def row_count(self) -> int:
        return len(self.ids)

    @property
    def column_count(self) -> int:
        return len(self.columns)

    @property
    def populated_count(self) -> int:
        return _popcount_bytes(self.participation)

    @property
    def zero_count(self) -> int:
        return self.populated_count - _popcount_bytes(self.votes)

    @property
    def one_count(self) -> int:
        return _popcount_bytes(self.votes)

    def _cell_index(self, row: int, column: int) -> int:
        if not isinstance(row, int) or not isinstance(column, int):
            raise TypeError("row and column must be integers")
        if row < 0 or row >= self.row_count:
            raise IndexError("row index out of range")
        if column < 0 or column >= self.column_count:
            raise IndexError("column index out of range")
        return row * self.column_count + column

    def has_vote(self, row: int, column: int) -> bool:
        index = self._cell_index(row, column)
        return bool(self.participation[index >> 3] & (1 << (index & 7)))

    def get_vote(self, row: int, column: int) -> int | None:
        index = self._cell_index(row, column)
        if not (self.participation[index >> 3] & (1 << (index & 7))):
            return None
        vote_index = _rank_before_with_index(self.participation, index, self._rank_index)
        return (self.votes[vote_index >> 3] >> (vote_index & 7)) & 1

    def get_row(self, row: int) -> list[str]:
        if not isinstance(row, int) or row < 0 or row >= self.row_count:
            raise IndexError("row index out of range")
        return [
            "" if not self.has_vote(row, column) else str(self.get_vote(row, column))
            for column in range(self.column_count)
        ]

    def to_matrix(self) -> list[list[str]]:
        return [self.get_row(row) for row in range(self.row_count)]


def _check_u64(value: int, what: str) -> None:
    if not isinstance(value, int) or value < 0 or value > U64_MAX:
        raise WVDBError(f"{what} is outside unsigned 64-bit range")


def _encode_uvarint(value: int) -> bytes:
    _check_u64(value, "uvarint")
    output = bytearray()
    while value >= 0x80:
        output.append((value & 0x7F) | 0x80)
        value >>= 7
    output.append(value)
    return bytes(output)


def _read_uvarint(data: bytes, offset: int, end: int, what: str = "uvarint") -> tuple[int, int]:
    value = 0
    shift = 0
    start = offset
    for _ in range(10):
        if offset >= end:
            raise WVDBError(f"truncated {what}")
        byte = data[offset]
        offset += 1
        payload = byte & 0x7F
        if shift == 63 and payload > 1:
            raise WVDBError(f"{what} overflows unsigned 64-bit range")
        value |= payload << shift
        if not byte & 0x80:
            if offset - start > 1 and value < (1 << (7 * (offset - start - 1))):
                raise WVDBError(f"non-canonical {what}")
            return value, offset
        shift += 7
    raise WVDBError(f"overlong {what}")


def _common_prefix(left: bytes, right: bytes) -> int:
    length = min(len(left), len(right))
    index = 0
    while index < length and left[index] == right[index]:
        index += 1
    return index


def _encode_strings(strings: Sequence[str], *, require_sorted: bool = False) -> bytes:
    output = bytearray()
    previous = b""
    for index, value in enumerate(strings):
        if not isinstance(value, str):
            raise WVDBError("string values are required")
        current = value.encode("utf-8", "strict")
        if len(current) > MAX_STRING_BYTES:
            raise WVDBError("string is too large")
        if require_sorted and index and current <= previous:
            raise WVDBError("nation IDs must be strictly increasing")
        prefix = 0 if index == 0 else _common_prefix(previous, current)
        output.extend(_encode_uvarint(prefix))
        output.extend(_encode_uvarint(len(current) - prefix))
        output.extend(current[prefix:])
        previous = current
    return bytes(output)


def _decode_strings(payload: bytes, count: int, *, require_sorted: bool, label: str) -> tuple[str, ...]:
    maximum = MAX_ROWS if label == "nation IDs" else MAX_COLUMNS
    if count > maximum:
        raise WVDBError(f"{label} count is too large")
    offset = 0
    previous = b""
    result: list[str] = []
    for index in range(count):
        prefix, offset = _read_uvarint(payload, offset, len(payload), f"{label} prefix length")
        suffix_length, offset = _read_uvarint(payload, offset, len(payload), f"{label} suffix length")
        if index == 0 and prefix != 0:
            raise WVDBError(f"first {label} prefix must be zero")
        if prefix > len(previous):
            raise WVDBError(f"{label} prefix is longer than previous value")
        if suffix_length > MAX_STRING_BYTES or suffix_length > len(payload) - offset:
            raise WVDBError(f"invalid {label} suffix length")
        if prefix > MAX_STRING_BYTES - suffix_length:
            raise WVDBError(f"{label} value is too large")
        current = previous[:prefix] + payload[offset : offset + suffix_length]
        offset += suffix_length
        if len(current) > MAX_STRING_BYTES:
            raise WVDBError(f"{label} value is too large")
        try:
            value = current.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WVDBError(f"invalid UTF-8 in {label}") from exc
        if require_sorted and index and current <= previous:
            raise WVDBError("nation IDs must be strictly increasing")
        result.append(value)
        previous = current
    if offset != len(payload):
        raise WVDBError(f"extra data in {label} block")
    return tuple(result)


def _pack_bitmap(bits: Iterable[int]) -> bytes:
    output = bytearray()
    current = 0
    bit = 0
    for value in bits:
        if value:
            current |= 1 << bit
        bit += 1
        if bit == 8:
            output.append(current)
            current = 0
            bit = 0
    if bit:
        output.append(current)
    return bytes(output)


def _bitmap_padding_is_zero(data: bytes, bit_count: int) -> bool:
    if not data or bit_count % 8 == 0:
        return True
    return data[-1] & (~((1 << (bit_count % 8)) - 1) & 0xFF) == 0


def _popcount_bytes(data: bytes) -> int:
    return sum(byte.bit_count() for byte in data)


def _build_rank_index(bitmap: bytes) -> tuple[int, ...]:
    index = [0]
    total = 0
    for offset in range(0, len(bitmap), RANK_STRIDE_BITS // 8):
        total += sum(byte.bit_count() for byte in bitmap[offset : offset + RANK_STRIDE_BITS // 8])
        index.append(total)
    return tuple(index)


def _rank_before_with_index(bitmap: bytes, bit_index: int, rank_index: tuple[int, ...]) -> int:
    block, remainder_bits = divmod(bit_index, RANK_STRIDE_BITS)
    offset = block * (RANK_STRIDE_BITS // 8)
    full_bytes, remainder = divmod(remainder_bits, 8)
    return rank_index[block] + sum(byte.bit_count() for byte in bitmap[offset : offset + full_bytes]) + (
        (bitmap[offset + full_bytes] & ((1 << remainder) - 1)).bit_count() if remainder else 0
    )


def _validate_table(
    columns: Sequence[str],
    ids: Sequence[str],
    participation: bytes,
    votes: bytes,
) -> None:
    row_count = len(ids)
    column_count = len(columns)
    if row_count > MAX_ROWS or column_count > MAX_COLUMNS:
        raise WVDBError("table dimensions exceed configured limits")
    if row_count and column_count > MAX_CELLS // row_count:
        raise WVDBError("table has too many cells")
    cell_count = row_count * column_count
    expected_participation = (cell_count + 7) // 8
    if len(participation) != expected_participation:
        raise WVDBError("incorrect participation bitmap length")
    populated = _popcount_bytes(participation)
    if not _bitmap_padding_is_zero(participation, cell_count):
        raise WVDBError("nonzero participation padding bits")
    expected_votes = (populated + 7) // 8
    if len(votes) != expected_votes:
        raise WVDBError("incorrect vote bitmap length")
    if not _bitmap_padding_is_zero(votes, populated):
        raise WVDBError("nonzero vote padding bits")
    for value in columns:
        if not isinstance(value, str):
            raise WVDBError("column names must be strings")
        if len(value.encode("utf-8", "strict")) > MAX_STRING_BYTES:
            raise WVDBError("column name is too large")
    previous = None
    for value in ids:
        if not isinstance(value, str):
            raise WVDBError("nation IDs must be strings")
        encoded = value.encode("utf-8", "strict")
        if len(encoded) > MAX_STRING_BYTES:
            raise WVDBError("nation ID is too large")
        if previous is not None and encoded <= previous:
            raise WVDBError("nation IDs must be strictly increasing")
        previous = encoded


def table_from_matrix(columns: Sequence[str], ids: Sequence[str], matrix: Sequence[Sequence[str]]) -> Table:
    if len(matrix) != len(ids):
        raise WVDBError("row count does not match nation ID count")
    participation_values: list[int] = []
    vote_values: list[int] = []
    for row in matrix:
        if len(row) != len(columns):
            raise WVDBError("row has the wrong number of columns")
        for value in row:
            if value not in ("", "0", "1"):
                raise WVDBError("vote cells must be empty, '0', or '1'")
            present = value != ""
            participation_values.append(int(present))
            if present:
                vote_values.append(int(value))
    participation = _pack_bitmap(participation_values)
    votes = _pack_bitmap(vote_values)
    return Table(tuple(columns), tuple(ids), participation, votes)


def read_csv(path: os.PathLike[str] | str) -> Table:
    previous_field_limit = csv.field_size_limit()
    csv.field_size_limit(MAX_STRING_BYTES)
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            try:
                header = next(reader)
            except StopIteration as exc:
                raise WVDBError("CSV is empty") from exc
            if not header or header[0] != "nation_id":
                raise WVDBError("CSV first header must be 'nation_id'")
            columns = header[1:]
            ids: list[str] = []
            matrix: list[list[str]] = []
            for row_number, row in enumerate(reader, start=2):
                if len(row) != len(header):
                    raise WVDBError(f"CSV row {row_number} has the wrong number of fields")
                ids.append(row[0])
                matrix.append(row[1:])
    except UnicodeDecodeError as exc:
        raise WVDBError("CSV is not valid UTF-8") from exc
    finally:
        csv.field_size_limit(previous_field_limit)
    return table_from_matrix(columns, ids, matrix)


def _make_block(block_type: int, payload: bytes) -> bytes:
    if len(payload) > MAX_BLOCK_PAYLOAD:
        raise WVDBError("block payload is too large")
    return bytes((block_type, 1)) + _encode_uvarint(len(payload)) + payload


def encode_table(table: Table, verify: bool = True) -> bytes:
    if not isinstance(table, Table):
        raise TypeError("encode_table expects a Table")
    columns = _encode_strings(table.columns)
    ids = _encode_strings(table.ids, require_sorted=True)
    blocks = b"".join(
        (
            _make_block(BLOCK_COLUMNS, columns),
            _make_block(BLOCK_IDS, ids),
            _make_block(BLOCK_PARTICIPATION, table.participation),
            _make_block(BLOCK_VOTES, table.votes),
        )
    )
    body = b"".join(
        (
            MAGIC,
            bytes((VERSION, FLAGS)),
            _encode_uvarint(table.row_count),
            _encode_uvarint(table.column_count),
            _encode_uvarint(len(REQUIRED_BLOCKS)),
            blocks,
        )
    )
    result = body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)
    if len(result) > MAX_WVDB_BYTES:
        raise WVDBError("WVDB output is too large")
    if verify and decode(result) != table:
        raise WVDBError("WVDB self-verification failed")
    return result


def _decode_bitmap(payload: bytes, expected_length: int, bit_count: int, label: str) -> bytes:
    if len(payload) != expected_length:
        raise WVDBError(f"incorrect {label} bitmap length")
    if not _bitmap_padding_is_zero(payload, bit_count):
        raise WVDBError(f"nonzero {label} bitmap padding bits")
    return payload


def decode(data: bytes | bytearray | memoryview) -> Table:
    data = bytes(data)
    if len(data) > MAX_WVDB_BYTES:
        raise WVDBError("WVDB file is too large")
    if len(data) < 4 + 2 + 1 + 1 + 1 + 4:
        raise WVDBError("WVDB file is truncated")
    if data[:4] != MAGIC:
        raise WVDBError("wrong WVDB magic")
    if data[4] != VERSION:
        raise WVDBError(f"unsupported WVDB version {data[4]}")
    if data[5] != FLAGS:
        raise WVDBError("unsupported WVDB flags")
    crc_offset = len(data) - 4
    expected_crc = struct.unpack_from("<I", data, crc_offset)[0]
    actual_crc = zlib.crc32(data[:crc_offset]) & 0xFFFFFFFF
    if actual_crc != expected_crc:
        raise WVDBError("WVDB CRC mismatch")

    offset = 6
    row_count, offset = _read_uvarint(data, offset, crc_offset, "row count")
    column_count, offset = _read_uvarint(data, offset, crc_offset, "column count")
    block_count, offset = _read_uvarint(data, offset, crc_offset, "block count")
    if row_count > MAX_ROWS:
        raise WVDBError("row count exceeds configured limit")
    if column_count > MAX_COLUMNS:
        raise WVDBError("column count exceeds configured limit")
    if row_count and column_count > MAX_CELLS // row_count:
        raise WVDBError("matrix has too many cells")
    if block_count > MAX_BLOCKS:
        raise WVDBError("block count exceeds configured limit")
    if block_count < len(REQUIRED_BLOCKS):
        raise WVDBError("required WVDB block is missing")
    cell_count = row_count * column_count
    expected_participation_length = (cell_count + 7) // 8
    if expected_participation_length > MAX_BLOCK_PAYLOAD:
        raise WVDBError("participation bitmap exceeds configured limit")

    blocks: dict[int, bytes] = {}
    for _ in range(block_count):
        if offset + 2 > crc_offset:
            raise WVDBError("truncated block header")
        block_type = data[offset]
        block_flags = data[offset + 1]
        offset += 2
        if block_flags & 0xFE:
            raise WVDBError("reserved block flags are nonzero")
        payload_length, offset = _read_uvarint(data, offset, crc_offset, "block payload length")
        if payload_length > MAX_BLOCK_PAYLOAD or payload_length > crc_offset - offset:
            raise WVDBError("block payload extends beyond WVDB data")
        payload = data[offset : offset + payload_length]
        offset += payload_length
        if block_type in REQUIRED_BLOCKS:
            if not block_flags & 1:
                raise WVDBError("required block is not marked critical")
            if block_type in blocks:
                raise WVDBError("duplicate required block")
            blocks[block_type] = payload
        elif block_flags & 1:
            raise WVDBError("unknown critical block")
    if offset != crc_offset:
        raise WVDBError("extra data after WVDB blocks")
    missing = [block_type for block_type in REQUIRED_BLOCKS if block_type not in blocks]
    if missing:
        raise WVDBError("missing required WVDB block")

    columns = _decode_strings(blocks[BLOCK_COLUMNS], column_count, require_sorted=False, label="columns")
    ids = _decode_strings(blocks[BLOCK_IDS], row_count, require_sorted=True, label="nation IDs")
    participation = _decode_bitmap(
        blocks[BLOCK_PARTICIPATION], expected_participation_length, cell_count, "participation"
    )
    populated = _popcount_bytes(participation)
    expected_vote_length = (populated + 7) // 8
    if expected_vote_length > MAX_BLOCK_PAYLOAD:
        raise WVDBError("vote bitmap exceeds configured limit")
    votes = _decode_bitmap(blocks[BLOCK_VOTES], expected_vote_length, populated, "vote")
    return Table(columns, ids, participation, votes)


def _atomic_write(path: os.PathLike[str] | str, data: bytes, *, text: bool = False) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if text else "wb"
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode=mode, dir=destination.parent, delete=False, encoding="utf-8" if text else None, newline="" if text else None) as handle:
            temp_name = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def encode_csv(input_path: os.PathLike[str] | str, output_path: os.PathLike[str] | str, *, verify: bool = True) -> Table:
    table = read_csv(input_path)
    data = encode_table(table, verify=verify)
    _atomic_write(output_path, data)
    return table


def _write_csv(path: os.PathLike[str] | str, table: Table) -> None:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("nation_id", *table.columns))
    for row, nation_id in enumerate(table.ids):
        writer.writerow((nation_id, *table.get_row(row)))
    _atomic_write(path, output.getvalue(), text=True)


def decode_to_csv(input_path: os.PathLike[str] | str, output_path: os.PathLike[str] | str) -> Table:
    with open(input_path, "rb") as handle:
        data = handle.read(MAX_WVDB_BYTES + 1)
    if len(data) > MAX_WVDB_BYTES:
        raise WVDBError("WVDB input is too large")
    table = decode(data)
    _write_csv(output_path, table)
    return table


def inspect_data(data: bytes) -> dict[str, int]:
    table = decode(data)
    block_sizes = {block_type: 0 for block_type in REQUIRED_BLOCKS}
    offset = 6
    _, offset = _read_uvarint(data, offset, len(data) - 4)
    _, offset = _read_uvarint(data, offset, len(data) - 4)
    block_count, offset = _read_uvarint(data, offset, len(data) - 4)
    for _ in range(block_count):
        block_type = data[offset]
        offset += 2
        payload_length, offset = _read_uvarint(data, offset, len(data) - 4)
        if block_type in block_sizes:
            block_sizes[block_type] = payload_length
        offset += payload_length
    result = {
        "version": VERSION,
        "rows": table.row_count,
        "columns": table.column_count,
        "populated": table.populated_count,
        "zeros": table.zero_count,
        "ones": table.one_count,
        "raw_wvdb_size": len(data),
        "column_block_size": block_sizes[BLOCK_COLUMNS],
        "id_block_size": block_sizes[BLOCK_IDS],
        "participation_block_size": block_sizes[BLOCK_PARTICIPATION],
        "vote_block_size": block_sizes[BLOCK_VOTES],
    }
    return result


def _print_inspect(stats: dict[str, int]) -> None:
    labels = (
        ("WVDB version", "version"),
        ("rows", "rows"),
        ("columns", "columns"),
        ("number of populated cells", "populated"),
        ("number of 0 votes", "zeros"),
        ("number of 1 votes", "ones"),
        ("raw WVDB size", "raw_wvdb_size"),
        ("column block size", "column_block_size"),
        ("ID block size", "id_block_size"),
        ("participation block size", "participation_block_size"),
        ("vote block size", "vote_block_size"),
    )
    for label, key in labels:
        print(f"{label}: {stats[key]}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Encode and decode WVDB vote tables")
    subparsers = parser.add_subparsers(dest="command", required=True)

    encode_parser = subparsers.add_parser("encode")
    encode_parser.add_argument("input_csv")
    encode_parser.add_argument("output_wvdb")
    encode_parser.add_argument("--no-verify", action="store_true")

    decode_parser = subparsers.add_parser("decode")
    decode_parser.add_argument("input_wvdb")
    decode_parser.add_argument("output_csv")

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("input_wvdb")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "encode":
            encode_csv(args.input_csv, args.output_wvdb, verify=not args.no_verify)
        elif args.command == "decode":
            decode_to_csv(args.input_wvdb, args.output_csv)
        elif args.command == "inspect":
            with open(args.input_wvdb, "rb") as handle:
                data = handle.read(MAX_WVDB_BYTES + 1)
            if len(data) > MAX_WVDB_BYTES:
                raise WVDBError("WVDB input is too large")
            stats = inspect_data(data)
            _print_inspect(stats)
        return 0
    except (OSError, WVDBError, csv.Error) as exc:
        print(f"WVDB error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
