"""run_query and get_npc_loot against a fake database connection."""

import pytest
from mcp import Client

from eqemu_mcp import tools_database, tools_entities, tools_entities_write
from eqemu_mcp.config import MAX_QUERY_ROWS
from eqemu_mcp.mcp_server import EQEmuMCPServer


class FakeCursor:
    def __init__(self, results):
        self.results = list(results)
        self.executed = []
        self.description = None
        self.lastrowid = 1
        self._rows = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        columns, rows = self.results.pop(0) if self.results else (["x"], [])
        self.description = [(c,) for c in columns]
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def close(self):
        pass


def fake_db(monkeypatch, module, *results):
    cursor = FakeCursor(results)
    monkeypatch.setattr(module, "db_conn", lambda: FakeConn(cursor))
    return cursor


async def call(register, tool, **args):
    server = EQEmuMCPServer("eqemu-test")
    register(server)
    async with Client(server) as client:
        return await client.call_tool(tool, args)


def text(result):
    return result.content[0].text


async def test_run_query_says_when_rows_were_cut_off(monkeypatch):
    rows = [(i,) for i in range(MAX_QUERY_ROWS + 1)]
    cursor = fake_db(monkeypatch, tools_database, (["id"], rows))

    result = await call(tools_database.register, "run_query", sql="SELECT id FROM npc_types", limit=5000)

    assert cursor.executed[0].endswith(f"LIMIT {MAX_QUERY_ROWS + 1}")
    assert f"truncated at {MAX_QUERY_ROWS} rows" in text(result)
    assert "LIMIT/OFFSET" in text(result)
    assert text(result).count("\n") < MAX_QUERY_ROWS + 10


async def test_run_query_without_cut_off_has_no_notice(monkeypatch):
    fake_db(monkeypatch, tools_database, (["id"], [(1,), (2,)]))

    result = await call(tools_database.register, "run_query", sql="SELECT id FROM npc_types")

    assert "truncated" not in text(result)
    assert text(result).endswith("(2 rows)")


async def test_run_query_caps_a_query_with_its_own_limit(monkeypatch):
    fake_db(monkeypatch, tools_database, (["id"], [(i,) for i in range(MAX_QUERY_ROWS + 50)]))

    result = await call(tools_database.register, "run_query", sql="SELECT id FROM npc_types LIMIT 1000", limit=5000)

    assert f"truncated at {MAX_QUERY_ROWS} rows" in text(result)


async def test_run_query_keeps_null_empty_zero_and_newlines_apart(monkeypatch):
    long_value = "y" * 200
    fake_db(monkeypatch, tools_database, (["a", "b", "c", "d", "e"], [(None, "", 0, "one\ntwo\tx", long_value)]))

    result = await call(tools_database.register, "run_query", sql="SELECT a,b,c,d,e FROM t")
    row = text(result).splitlines()[2]

    assert [cell.strip() for cell in row.split(" | ")] == ["NULL", "", "0", "one\\ntwo\\tx", long_value]


async def test_run_query_refusals_are_errors(monkeypatch):
    fake_db(monkeypatch, tools_database)

    for sql in ("UPDATE npc_types SET hp=1", "SELECT 1; DELETE FROM npc_types", "SELECT 1 INTO OUTFILE '/tmp/x'"):
        result = await call(tools_database.register, "run_query", sql=sql)
        assert result.is_error is True, sql


async def test_get_npc_loot_uses_columns_eqemu_has(monkeypatch):
    cursor = fake_db(
        monkeypatch,
        tools_entities,
        (["name", "loottable_id"], [("a_rat", 77)]),
        (
            ["lt_id", "lt_name", "lootdrop_id", "drop_name", "multiplier", "probability", "droplimit", "mindrop",
             "item_id", "item_name", "chance", "item_multiplier", "item_charges", "equip_item"],
            [(77, "rat_loot", 5, "rat_drop", 1, 100, 0, 0, 13073, "Bone Chips", 25.0, 2, 1, 0),
             (77, "rat_loot", 5, "rat_drop", 1, 100, 0, 0, 99999, None, 5.0, 1, 1, 0)],
        ),
    )

    result = await call(tools_entities.register, "get_npc_loot", npc_id=1)

    assert result.is_error is False
    assert "min_amount" not in cursor.executed[1]
    assert "LEFT JOIN items" in cursor.executed[1]
    assert "[13073] Bone Chips" in text(result)
    assert "chance=25.0% x2" in text(result)
    assert "[99999] (no such item)" in text(result)


async def test_add_loot_to_npc_writes_columns_eqemu_has(monkeypatch):
    cursor = fake_db(
        monkeypatch,
        tools_entities_write,
        (["name", "loottable_id"], [("a_rat", 77)]),
        (["Name"], [("Bone Chips",)]),
    )

    result = await call(tools_entities_write.register_write, "add_loot_to_npc", npc_id=1, item_id=13073, multiplier=3)

    assert result.is_error is False
    inserts = [sql for sql in cursor.executed if "lootdrop_entries" in sql]
    assert inserts and "min_amount" not in inserts[0] and "multiplier" in inserts[0]
    assert "x3" in text(result)


@pytest.fixture(autouse=True)
def _no_real_database(monkeypatch):
    def refuse():
        raise AssertionError("test reached a real database")

    for module in (tools_database, tools_entities, tools_entities_write):
        monkeypatch.setattr(module, "db_conn", refuse)
