(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.WVDB = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const MAGIC = [0x57, 0x56, 0x44, 0x42];
  const VERSION = 1;
  const RANK_STRIDE_BITS = 512;
  const MAX_ROWS = 10000000;
  const MAX_COLUMNS = 1000000;
  const MAX_CELLS = 100000000;
  const MAX_STRING_BYTES = 64 * 1024 * 1024;
  const MAX_BLOCKS = 1000000;
  const MAX_BLOCK_PAYLOAD = 256 * 1024 * 1024;
  const MAX_WVDB_BYTES = 256 * 1024 * 1024;
  const REQUIRED = [1, 2, 3, 4];

  class WVDBError extends Error {
    constructor(message) {
      super(message);
      this.name = "WVDBError";
    }
  }

  function asBytes(input) {
    if (input instanceof Uint8Array) return input;
    if (input instanceof ArrayBuffer) return new Uint8Array(input);
    if (typeof ArrayBuffer !== "undefined" && ArrayBuffer.isView(input)) {
      return new Uint8Array(input.buffer, input.byteOffset, input.byteLength);
    }
    throw new TypeError("decodeWVDB expects an ArrayBuffer or Uint8Array");
  }

  function readUVarint(bytes, state, end, label) {
    let value = 0;
    let shift = 0;
    const start = state.offset;
    for (let count = 0; count < 10; count += 1) {
      if (state.offset >= end) throw new WVDBError("truncated " + label);
      const byte = bytes[state.offset++];
      const payload = byte & 0x7f;
      if (shift === 49 && payload > 15) throw new WVDBError(label + " exceeds safe integer range");
      if (shift > 49 && payload !== 0) throw new WVDBError(label + " exceeds safe integer range");
      value += payload * 2 ** shift;
      if ((byte & 0x80) === 0) {
        if (state.offset - start > 1 && value < 2 ** (7 * (state.offset - start - 1))) {
          throw new WVDBError("non-canonical " + label);
        }
        return value;
      }
      shift += 7;
    }
    throw new WVDBError("overlong " + label);
  }

  function readStrings(payload, count, sorted, label) {
    const state = { offset: 0 };
    let previousBytes = new Uint8Array(0);
    const strings = [];
    const decoder = new TextDecoder("utf-8", { fatal: true });
    const maximum = label === "nation IDs" ? MAX_ROWS : MAX_COLUMNS;
    if (count > maximum) throw new WVDBError(label + " count is too large");

    for (let index = 0; index < count; index += 1) {
      const prefix = readUVarint(payload, state, payload.length, label + " prefix length");
      const suffixLength = readUVarint(payload, state, payload.length, label + " suffix length");
      if (index === 0 && prefix !== 0) throw new WVDBError("first " + label + " prefix must be zero");
      if (prefix > previousBytes.length) throw new WVDBError(label + " prefix is longer than previous value");
      if (suffixLength > MAX_STRING_BYTES || suffixLength > payload.length - state.offset) {
        throw new WVDBError("invalid " + label + " suffix length");
      }
      if (prefix > MAX_STRING_BYTES - suffixLength) throw new WVDBError(label + " value is too large");
      const current = new Uint8Array(prefix + suffixLength);
      current.set(previousBytes.subarray(0, prefix), 0);
      current.set(payload.subarray(state.offset, state.offset + suffixLength), prefix);
      state.offset += suffixLength;
      if (current.length > MAX_STRING_BYTES) throw new WVDBError(label + " value is too large");
      let value;
      try {
        value = decoder.decode(current);
      } catch (error) {
        throw new WVDBError("invalid UTF-8 in " + label);
      }
      if (sorted && index > 0 && compareBytes(current, previousBytes) <= 0) {
        throw new WVDBError("nation IDs must be strictly increasing");
      }
      strings.push(value);
      previousBytes = current;
    }
    if (state.offset !== payload.length) throw new WVDBError("extra data in " + label + " block");
    return strings;
  }

  function compareBytes(left, right) {
    const length = Math.min(left.length, right.length);
    for (let index = 0; index < length; index += 1) {
      if (left[index] !== right[index]) return left[index] < right[index] ? -1 : 1;
    }
    return left.length === right.length ? 0 : left.length < right.length ? -1 : 1;
  }

  function popcountByte(value) {
    value = value - ((value >>> 1) & 0x55);
    value = (value & 0x33) + ((value >>> 2) & 0x33);
    return ((value + (value >>> 4)) & 0x0f);
  }

  function popcount(bytes) {
    let total = 0;
    for (let index = 0; index < bytes.length; index += 1) total += popcountByte(bytes[index]);
    return total;
  }

  function paddingIsZero(bytes, bitCount) {
    if (!bytes.length || bitCount % 8 === 0) return true;
    const mask = 0xff ^ ((1 << (bitCount % 8)) - 1);
    return (bytes[bytes.length - 1] & mask) === 0;
  }

  function crc32(bytes, end) {
    let crc = 0xffffffff;
    for (let index = 0; index < end; index += 1) {
      crc ^= bytes[index];
      for (let bit = 0; bit < 8; bit += 1) {
        crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
      }
    }
    return (crc ^ 0xffffffff) >>> 0;
  }

  class Table {
    constructor(columns, ids, participation, votes) {
      this.columns = Object.freeze(columns.slice());
      this.ids = Object.freeze(ids.slice());
      this.participation = participation;
      this.votes = votes;
      this.rowCount = this.ids.length;
      this.columnCount = this.columns.length;
      this._rankIndex = buildRankIndex(this.participation);
      this.populatedCount = popcount(this.participation);
      this.zeroCount = this.populatedCount - popcount(this.votes);
      this.oneCount = popcount(this.votes);
    }

    _cellIndex(row, column) {
      if (!Number.isSafeInteger(row) || !Number.isSafeInteger(column)) {
        throw new TypeError("row and column must be integers");
      }
      if (row < 0 || row >= this.rowCount) throw new RangeError("row index out of range");
      if (column < 0 || column >= this.columnCount) throw new RangeError("column index out of range");
      return row * this.columnCount + column;
    }

    hasVote(row, column) {
      const index = this._cellIndex(row, column);
      return (this.participation[index >>> 3] & (1 << (index & 7))) !== 0;
    }

    getVote(row, column) {
      const index = this._cellIndex(row, column);
      if ((this.participation[index >>> 3] & (1 << (index & 7))) === 0) return null;
      const voteIndex = rankBefore(this.participation, index, this._rankIndex);
      return (this.votes[voteIndex >>> 3] >>> (voteIndex & 7)) & 1;
    }

    getRow(row) {
      if (!Number.isSafeInteger(row) || row < 0 || row >= this.rowCount) {
        throw new RangeError("row index out of range");
      }
      const values = [];
      for (let column = 0; column < this.columnCount; column += 1) {
        const vote = this.getVote(row, column);
        values.push(vote === null ? "" : String(vote));
      }
      return values;
    }
  }

  function buildRankIndex(bitmap) {
    const bytesPerStride = RANK_STRIDE_BITS / 8;
    const result = new Uint32Array(Math.ceil(bitmap.length / bytesPerStride) + 1);
    let total = 0;
    for (let offset = 0, index = 0; offset < bitmap.length; offset += bytesPerStride, index += 1) {
      result[index] = total;
      const end = Math.min(offset + bytesPerStride, bitmap.length);
      for (let cursor = offset; cursor < end; cursor += 1) total += popcountByte(bitmap[cursor]);
    }
    result[result.length - 1] = total;
    return result;
  }

  function rankBefore(bitmap, bitIndex, rankIndex) {
    const block = Math.floor(bitIndex / RANK_STRIDE_BITS);
    const within = bitIndex - block * RANK_STRIDE_BITS;
    const offset = block * (RANK_STRIDE_BITS / 8);
    const fullBytes = Math.floor(within / 8);
    let total = rankIndex[block];
    for (let index = 0; index < fullBytes; index += 1) total += popcountByte(bitmap[offset + index]);
    const remainder = within & 7;
    if (remainder) total += popcountByte(bitmap[offset + fullBytes] & ((1 << remainder) - 1));
    return total;
  }

  function decodeWVDB(input) {
    const bytes = asBytes(input);
    if (bytes.length > MAX_WVDB_BYTES) throw new WVDBError("WVDB file is too large");
    if (bytes.length < 13) throw new WVDBError("WVDB file is truncated");
    for (let index = 0; index < MAGIC.length; index += 1) {
      if (bytes[index] !== MAGIC[index]) throw new WVDBError("wrong WVDB magic");
    }
    if (bytes[4] !== VERSION) throw new WVDBError("unsupported WVDB version " + bytes[4]);
    if (bytes[5] !== 0) throw new WVDBError("unsupported WVDB flags");
    const crcOffset = bytes.length - 4;
    const expectedCrc = (bytes[crcOffset] | (bytes[crcOffset + 1] << 8) | (bytes[crcOffset + 2] << 16) | (bytes[crcOffset + 3] << 24)) >>> 0;
    if (crc32(bytes, crcOffset) !== expectedCrc) throw new WVDBError("WVDB CRC mismatch");

    const state = { offset: 6 };
    const rows = readUVarint(bytes, state, crcOffset, "row count");
    const columns = readUVarint(bytes, state, crcOffset, "column count");
    const blockCount = readUVarint(bytes, state, crcOffset, "block count");
    if (rows > MAX_ROWS || columns > MAX_COLUMNS) throw new WVDBError("table dimensions exceed configured limits");
    if (rows !== 0 && columns > Math.floor(MAX_CELLS / rows)) throw new WVDBError("matrix has too many cells");
    if (blockCount > MAX_BLOCKS || blockCount < REQUIRED.length) throw new WVDBError("invalid block count");
    const cellCount = rows * columns;
    const participationLength = Math.ceil(cellCount / 8);
    if (participationLength > MAX_BLOCK_PAYLOAD) throw new WVDBError("participation bitmap exceeds configured limit");

    const blocks = new Map();
    for (let index = 0; index < blockCount; index += 1) {
      if (state.offset + 2 > crcOffset) throw new WVDBError("truncated block header");
      const type = bytes[state.offset++];
      const flags = bytes[state.offset++];
      if ((flags & 0xfe) !== 0) throw new WVDBError("reserved block flags are nonzero");
      const length = readUVarint(bytes, state, crcOffset, "block payload length");
      if (length > MAX_BLOCK_PAYLOAD || length > crcOffset - state.offset) {
        throw new WVDBError("block payload extends beyond WVDB data");
      }
      const payload = bytes.subarray(state.offset, state.offset + length);
      state.offset += length;
      if (REQUIRED.indexOf(type) !== -1) {
        if ((flags & 1) === 0) throw new WVDBError("required block is not marked critical");
        if (blocks.has(type)) throw new WVDBError("duplicate required block");
        blocks.set(type, payload);
      } else if ((flags & 1) !== 0) {
        throw new WVDBError("unknown critical block");
      }
    }
    if (state.offset !== crcOffset) throw new WVDBError("extra data after WVDB blocks");
    for (const type of REQUIRED) if (!blocks.has(type)) throw new WVDBError("missing required WVDB block");

    const decodedColumns = readStrings(blocks.get(1), columns, false, "columns");
    const decodedIds = readStrings(blocks.get(2), rows, true, "nation IDs");
    const participation = blocks.get(3);
    if (participation.length !== participationLength) throw new WVDBError("incorrect participation bitmap length");
    if (!paddingIsZero(participation, cellCount)) throw new WVDBError("nonzero participation bitmap padding bits");
    const populated = popcount(participation);
    const voteLength = Math.ceil(populated / 8);
    if (voteLength > MAX_BLOCK_PAYLOAD) throw new WVDBError("vote bitmap exceeds configured limit");
    const votes = blocks.get(4);
    if (votes.length !== voteLength) throw new WVDBError("incorrect vote bitmap length");
    if (!paddingIsZero(votes, populated)) throw new WVDBError("nonzero vote bitmap padding bits");
    return new Table(decodedColumns, decodedIds, participation, votes);
  }

  return { decode: decodeWVDB, decodeWVDB, Table, WVDBError };
});
