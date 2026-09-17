"""The three ways Fusion reads a slice out of Warp."""

import io
import json

import pyarrow as pa
import pytest

from fusion.adapters.outbound.warp.streams import (
    PAGED_IN_CHUNK,
    ArrowRowStream,
    NdjsonRowStream,
    PagedRowStream,
)
from fusion.domain.errors import QueryError
from fusion.domain.slices import Predicate, SliceSpec
from tests.fakes.warp_transport import FakeByteStream

ROWS = [
    {"id": 1, "name": "Alice", "score": 9.5},
    {"id": 2, "name": "Bob", "score": None},
    {"id": 3, "name": "Cara", "score": 7.0},
]


def _arrow_bytes(rows, schema=None):
    table = pa.Table.from_pylist(rows, schema=schema)
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue()


def _arrow_stream(rows, schema=None):
    response = FakeByteStream(_arrow_bytes(rows, schema))
    return ArrowRowStream(pa.ipc.open_stream(response.raw), response), response


class TestArrowRowStream:
    def test_columns_and_typed_schema(self):
        schema = pa.schema(
            [("id", pa.int32()), ("name", pa.string()), ("amount", pa.decimal128(10, 2))]
        )
        stream, _ = _arrow_stream([{"id": 1, "name": "a", "amount": None}], schema)
        assert stream.columns == ("id", "name", "amount")
        types = {c.name: c.type for c in stream.schema.columns}
        assert types == {"id": "integer", "name": "varchar", "amount": "decimal"}

    def test_timestamps_and_binaries_are_recognised(self):
        schema = pa.schema(
            [("ts", pa.timestamp("us", tz="UTC")), ("d", pa.date32()), ("b", pa.binary())]
        )
        stream, _ = _arrow_stream([], schema)
        assert [c.type for c in stream.schema.columns] == ["timestamp", "date", "blob"]

    def test_iteration_yields_rowsets(self):
        stream, _ = _arrow_stream(ROWS)
        batches = list(stream)
        assert [len(b) for b in batches] == [3]
        assert batches[0].columns == ("id", "name", "score")
        assert batches[0].rows[1] == (2, "Bob", None)

    def test_arrow_reader_is_handed_over_once(self):
        stream, _ = _arrow_stream(ROWS)
        reader = stream.arrow_reader()
        assert reader is not None
        assert stream.arrow_reader() is None
        assert list(stream) == []  # the reader belongs to the caller now
        assert reader.read_all().num_rows == 3

    def test_close_releases_the_response(self):
        stream, response = _arrow_stream(ROWS)
        stream.close()
        assert response.closed


class TestNdjsonRowStream:
    def _stream(self, rows, batch_size=10):
        body = "".join(json.dumps(r) + "\n" for r in rows).encode()
        response = FakeByteStream(body)
        return NdjsonRowStream(response, batch_size=batch_size), response

    def test_batches_and_column_order(self):
        stream, _ = self._stream(ROWS, batch_size=2)
        batches = list(stream)
        assert [len(b) for b in batches] == [2, 1]
        assert all(b.columns == ("id", "name", "score") for b in batches)
        assert stream.columns == ("id", "name", "score")
        assert batches[1].rows == [(3, "Cara", 7.0)]

    def test_missing_keys_become_null_in_the_shared_column_order(self):
        stream, _ = self._stream([{"id": 1, "name": "a"}, {"id": 2}], batch_size=1)
        batches = list(stream)
        assert batches[1].columns == ("id", "name")
        assert batches[1].rows == [(2, None)]

    def test_blank_lines_are_skipped_and_empty_bodies_yield_nothing(self):
        response = FakeByteStream(b'{"id": 1}\n\n')
        assert [len(b) for b in NdjsonRowStream(response)] == [1]
        assert list(NdjsonRowStream(FakeByteStream(b""))) == []

    def test_malformed_json_is_a_query_error(self):
        stream, _ = self._stream(ROWS)
        stream._response = FakeByteStream(b"{not json}\n")
        with pytest.raises(QueryError, match="NDJSON"):
            list(stream)

    def test_no_arrow_fast_path_and_close(self):
        stream, response = self._stream(ROWS)
        assert stream.arrow_reader() is None
        assert stream.schema is None
        stream.close()
        assert response.closed


class _Pager:
    """Records page requests and serves them out of a table."""

    def __init__(self, rows, shape="items"):
        self.rows = rows
        self.shape = shape
        self.calls = []

    def __call__(self, table, limit, offset=0, fields=None, filters=()):
        self.calls.append(
            {
                "table": table,
                "limit": limit,
                "offset": offset,
                "fields": fields,
                "filters": tuple(filters),
            }
        )
        rows = self.rows
        for predicate in filters:
            rows = [r for r in rows if predicate.matches(r)]
        if fields:
            rows = [{f: r.get(f) for f in fields} for r in rows]
        page = rows[offset : offset + limit]
        return {"items": page, "total": len(rows)} if self.shape == "items" else page


class TestPagedRowStream:
    def test_pages_until_the_end(self):
        pager = _Pager([{"id": i} for i in range(5)])
        stream = PagedRowStream(pager, "t", page_size=2)
        assert [len(b) for b in stream] == [2, 2, 1]
        assert [c["offset"] for c in pager.calls] == [0, 2, 4]
        assert stream.columns == ("id",)

    def test_projection_and_filters_go_to_the_server(self):
        pager = _Pager([{"id": 1, "s": "new"}, {"id": 2, "s": "old"}])
        spec = SliceSpec(columns=frozenset({"id"}), predicates=(Predicate("s", "eq", "new"),))
        rows = [r for batch in PagedRowStream(pager, "t", spec) for r in batch.rows]
        assert rows == [(1,)]
        assert pager.calls[0]["fields"] == ["id"]
        assert pager.calls[0]["filters"] == (Predicate("s", "eq", "new"),)

    def test_max_rows_stops_early_even_if_the_server_ignores_the_limit(self):
        pager = _Pager([{"id": i} for i in range(10)], shape="list")
        stream = PagedRowStream(pager, "t", page_size=4, max_rows=3)
        assert sum(len(b) for b in stream) == 3
        assert pager.calls[0]["limit"] == 3
        assert len(pager.calls) == 1

    def test_spec_limit_is_honoured(self):
        pager = _Pager([{"id": i} for i in range(10)])
        spec = SliceSpec(limit=2)
        assert sum(len(b) for b in PagedRowStream(pager, "t", spec, page_size=5)) == 2

    def test_long_in_lists_are_split_into_several_requests(self):
        rows = [{"id": i} for i in range(PAGED_IN_CHUNK * 2 + 5)]
        keys = tuple(r["id"] for r in rows)
        spec = SliceSpec(predicates=(Predicate("id", "in", keys),))
        pager = _Pager(rows)
        total = sum(len(b) for b in PagedRowStream(pager, "t", spec, page_size=1000))
        assert total == len(rows)
        chunk_sizes = [len(c["filters"][0].value) for c in pager.calls]
        assert chunk_sizes[0] == PAGED_IN_CHUNK
        assert sum(chunk_sizes) >= len(keys)

    def test_other_predicates_ride_along_with_every_chunk(self):
        keys = tuple(range(PAGED_IN_CHUNK + 1))
        spec = SliceSpec(predicates=(Predicate("id", "in", keys), Predicate("s", "eq", "new")))
        pager = _Pager([{"id": i, "s": "new"} for i in keys])
        list(PagedRowStream(pager, "t", spec, page_size=1000))
        assert all(Predicate("s", "eq", "new") in c["filters"] for c in pager.calls)

    def test_empty_result_still_yields_one_batch(self):
        pager = _Pager([])
        batches = list(PagedRowStream(pager, "t"))
        assert len(batches) == 1 and batches[0].is_empty

    def test_no_arrow_fast_path(self):
        stream = PagedRowStream(_Pager([]), "t")
        assert stream.arrow_reader() is None
        assert stream.schema is None
        assert stream.close() is None
