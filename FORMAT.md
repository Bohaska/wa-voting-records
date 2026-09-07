# WVDB version 1

WVDB is a deterministic binary container for a rectangular vote table. It is
designed to be easy to validate and decode in a browser, rather than to use a
specialized compression algorithm. `WVDB.py` implements the encoder, decoder,
and CSV command line. `WVDB.js` implements the browser decoder.

## Source table

The source CSV has a header whose first field is exactly `nation_id`. The
remaining header fields are column names. Every later row has the same field
count; its first field is a nation ID and every other field is exactly one of
the three strings: the empty string, `0`, or `1`. CSV quoting is handled by the normal RFC
4180-style CSV parser. Text is UTF-8 and is not Unicode-normalized.

The encoder requires nation IDs to be strictly increasing and rejects a row
that is out of order or duplicated. Columns may be in any order and may have
duplicate names.

## Integer encoding

Every integer except the CRC32 is an unsigned LEB128/uvarint. The low seven
bits of each byte are payload; bit 7 means another byte follows. Encodings
must be canonical: zero is `00`, and an integer must not use unnecessary
continuation bytes. Version-1 implementations accept at most ten bytes and
reject values outside the unsigned 64-bit range.

Multi-byte fixed-width integers are little-endian.

## Container

The complete uncompressed file is:

```text
magic          4 bytes: ASCII "WVDB"
version        u8: 1
flags          u8: 0
row_count      uvarint
column_count   uvarint
block_count    uvarint
blocks         block_count block records
crc32          u32 little-endian
```

The CRC32 is the ordinary IEEE CRC-32, with initial value `0xffffffff` and
final XOR `0xffffffff`. It covers every byte from `magic` through the final
block payload, excluding the four CRC bytes.

For version 1, top-level flags must be zero. The matrix has
`row_count * column_count` cells; implementations must check dimensions and
allocation bounds before multiplying or allocating based on untrusted input.

## Blocks

Each block is:

```text
block_type       u8
block_flags      u8
payload_length   uvarint
payload          payload_length bytes
```

Only block-flags bit 0 is defined. It is the critical bit; all other bits
must be zero. The four required version-1 blocks are critical:

| Type | Meaning |
| ---: | --- |
| `0x01` | column names |
| `0x02` | nation IDs |
| `0x03` | participation bitmap |
| `0x04` | vote bitmap |

An unknown non-critical block is skipped. An unknown critical block, a
duplicate required block, a missing required block, a reserved block flag, or
a required block without its critical bit is an error. Block payloads must fit
inside the bytes before the CRC, and there must be no extra bytes after the
last block and before the CRC.

## Front-coded strings

The columns block contains exactly `column_count` strings. The IDs block
contains exactly `row_count` strings. Each string is encoded as:

```text
common_prefix_length   uvarint
suffix_byte_length     uvarint
suffix_bytes           raw UTF-8 bytes
```

The prefix and suffix lengths count UTF-8 bytes, not Unicode characters. The
first string has a zero prefix. Every later string reconstructs as:

```text
previous_utf8_bytes[:common_prefix_length] + suffix_bytes
```

The prefix may not be longer than the previous byte string. The reconstructed
bytes must be valid UTF-8 and the payload must be consumed exactly.

Nation IDs are compared as UTF-8 byte strings (UTF-8 preserves Unicode scalar
value order), and each ID must be strictly greater than its predecessor. No
row permutation is stored.

## Participation bitmap

The participation block contains exactly:

```text
ceil(row_count * column_count / 8)
```

bytes. Cells are visited in row-major order. Cell index `i` is represented by
byte `i >> 3`, bit `i & 7`; bit values are least-significant-bit first. A zero
bit means the CSV cell was blank and a one bit means it contained `0` or `1`.
Unused high bits in the final byte must be zero. For a zero-cell matrix the
payload is empty.

## Vote bitmap

The vote block has one bit only for each populated cell, in the same row-major
scan. A populated cell consumes one vote bit: zero means CSV value `0`, and
one means CSV value `1`. If `P` is the number of set bits in the participation
bitmap, the payload length is exactly `ceil(P / 8)`. Its unused high padding
bits must be zero.

To decode a cell, first read its participation bit. If it is zero, return the
blank string. Otherwise, count set participation bits before that cell and use
that count as the index into the vote bitmap.

## Empty and boundary tables

Zero rows and/or zero columns are valid. A zero-column table has empty matrix
blocks but can still contain sorted nation IDs. A zero-row table can still
contain column names. There is no fixed current-dataset dimension in the
format.

## Reference command line

```text
python WVDB.py encode votes.csv votes.wvdb
python WVDB.py decode votes.wvdb output.csv
python WVDB.py inspect votes.wvdb
```

Encoding verifies its own output by decoding it and comparing columns, IDs,
and every compact bitmap-derived cell. `--no-verify` is available on the
encode command for benchmarking only. Output is written to a temporary file,
fsynced, and atomically renamed into place. GitHub Pages can apply HTTP
transport compression to the static file; WVDB itself has no second
compression wrapper.

## Browser API

```javascript
const table = WVDB.decode(arrayBuffer);
table.hasVote(row, column);       // boolean
table.getVote(row, column);       // null, 0, or 1
table.getRow(row);                // ["", "0", "1", ...]
```

The decoder keeps `columns`, `ids`, `participation`, and `votes` compact and
builds a cumulative popcount index every 512 participation bits. Thus a
random populated cell does not rescan the complete participation bitmap and
the matrix is never expanded into millions of JavaScript objects.

## Validation and allocation limits

The reference implementations reject wrong magic/version/flags, malformed or
overlong uvarints, integer overflow, truncated or oversized blocks, CRC
mismatches, invalid UTF-8, invalid nation ordering, wrong block counts,
incorrect bitmap lengths, nonzero padding, and trailing bytes. They also put
explicit upper bounds on dimensions, strings, blocks, payloads, and total
WVDB size before allocating from untrusted values. These limits are decoder
resource-safety limits, not changes to the logical table model.

## Supplied dataset measurement

Measured from the repository's current `votes.csv` (29,528 rows × 101
columns):

| Representation | Bytes |
| --- | ---: |
| raw CSV | 3,807,048 |
| raw WVDB | 700,216 |

The WVDB data counts are 414,207 populated cells, comprising 125,025 zero
votes and 289,182 one votes. The raw WVDB block payload sizes are: columns
2,374 bytes, IDs 273,241 bytes, participation 372,791 bytes, and votes
51,776 bytes. The raw WVDB total also includes its headers, block framing, and
CRC.
