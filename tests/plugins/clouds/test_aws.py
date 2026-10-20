from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from core.interfaces import (
    NotConfigured,
    ProviderAuthError,
    ProviderError,
    ProviderKind,
    ProviderUnavailable,
    QuotaExhausted,
    QuotaLimit,
)
from plugins.aws.dynamodb import DynamoDBAdapter, from_attr, to_attr


class FakeClientError(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": "boom"}, "ResponseMetadata": {"HTTPStatusCode": status}}


class EndpointConnectionError(Exception):
    pass


class FakeDynamo:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.client_kwargs: dict[str, Any] = {}
        self.closed = False
        self.fail: Exception | None = None

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, kwargs))
        if self.fail is not None:
            raise self.fail

    def list_tables(self, **kwargs: Any) -> dict[str, Any]:
        self._record("list_tables", kwargs)
        if kwargs.get("ExclusiveStartTableName") == "notes":
            return {"TableNames": ["users"]}
        if kwargs.get("Limit") == 1:
            return {"TableNames": ["notes"], "LastEvaluatedTableName": "notes"}
        return {"TableNames": ["notes"], "LastEvaluatedTableName": "notes"}

    def describe_table(self, **kwargs: Any) -> dict[str, Any]:
        self._record("describe_table", kwargs)
        name = kwargs["TableName"]
        return {"Table": {
            "TableName": name, "ItemCount": 7, "TableSizeBytes": 1000 if name == "notes" else 24,
            "AttributeDefinitions": [{"AttributeName": "sk", "AttributeType": "N"},
                                     {"AttributeName": "pk", "AttributeType": "S"}],
            "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
        }}

    def execute_statement(self, **kwargs: Any) -> dict[str, Any]:
        self._record("execute_statement", kwargs)
        if kwargs["Statement"].startswith("INSERT"):
            return {"Items": []}
        if kwargs.get("NextToken") == "n2":
            return {"Items": [{"pk": {"S": "c"}, "sk": {"N": "3"}}]}
        return {"Items": [{"pk": {"S": "a"}, "sk": {"N": "1"}, "tags": {"SS": ["y", "x"]}},
                          {"pk": {"S": "b"}, "sk": {"N": "2.5"}, "data": {"M": {"ok": {"BOOL": True}}}}],
                "NextToken": "n2"}

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_boto3(monkeypatch) -> FakeDynamo:
    fake = FakeDynamo()
    module = types.ModuleType("boto3")

    def client(service: str, **kwargs: Any) -> FakeDynamo:
        assert service == "dynamodb"
        fake.client_kwargs = kwargs
        return fake

    module.client = client
    monkeypatch.setitem(sys.modules, "boto3", module)
    return fake


SECRETS = {"access_key_id": "AKID", "secret_access_key": "SECRET"}


def test_attribute_codec() -> None:
    value = {"s": "x", "n": 3, "f": 1.5, "b": False, "z": None, "l": [1, "a"], "m": {"k": "v"}}
    assert to_attr(value)["M"]["n"] == {"N": "3"}
    assert from_attr(to_attr(value)) == value
    assert to_attr({"a", "b"})["SS"] and to_attr({1, 2})["NS"]
    assert from_attr({"B": b"\x00\x01"}) == "AAE="
    with pytest.raises(ProviderError):
        to_attr(object())


async def test_dynamodb_operations(make_ctx, fake_boto3) -> None:
    adapter = DynamoDBAdapter(make_ctx("aws_dynamodb", ProviderKind.DB, settings={"region": "eu-west-1"},
                              secrets=SECRETS, limits=[QuotaLimit(metric="bytes", window="total", limit=26843545600)]))
    assert (await adapter.health()).ok
    assert fake_boto3.client_kwargs == {"region_name": "eu-west-1", "aws_access_key_id": "AKID",
                                        "aws_secret_access_key": "SECRET"}
    assert [t.name for t in await adapter.list_tables()] == ["notes", "users"]
    info = await adapter.describe("notes")
    assert [c.name for c in info.columns] == ["pk", "sk"] and info.columns[0].type == "string"
    assert info.columns[0].primary_key and info.row_count == 7 and info.size_bytes == 1000

    res = await adapter.query('SELECT * FROM "notes" WHERE pk = ?;', ["a"])
    assert res.columns == ["pk", "sk", "tags", "data"] and res.row_count == 3 and not res.truncated
    assert res.rows[0] == ["a", 1, ["x", "y"], None] and res.rows[1][1] == 2.5 and res.rows[1][3] == {"ok": True}
    first_call = next(c for c in fake_boto3.calls if c[0] == "execute_statement")[1]
    assert first_call["Statement"] == 'SELECT * FROM "notes" WHERE pk = ?' and first_call["Parameters"] == [{"S": "a"}]
    limited = await adapter.query('SELECT * FROM "notes"', limit=1)
    assert limited.row_count == 1 and limited.truncated
    with pytest.raises(ProviderError):
        await adapter.query("SELECT 1", {"named": 1})
    calls_before = len(fake_boto3.calls)
    with pytest.raises(ProviderError, match="read|SELECT"):
        await adapter.query("DELETE FROM \"notes\" WHERE pk = ?", ["a"])
    assert len(fake_boto3.calls) == calls_before

    out = await adapter.execute("INSERT INTO \"notes\" VALUE {'pk': ?, 'sk': ?}", ["z", 9])
    assert out.affected_rows == 1
    usage = await adapter.usage()
    assert usage.used == 1024 and usage.extra["tables"] == {"notes": 1000, "users": 24}
    await adapter.close()
    assert fake_boto3.closed


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FakeClientError("ProvisionedThroughputExceededException", 400), QuotaExhausted),
        (FakeClientError("UnrecognizedClientException", 400), ProviderAuthError),
        (FakeClientError("InternalServerError", 500), ProviderUnavailable),
        (EndpointConnectionError("no route"), ProviderUnavailable),
    ],
)
async def test_dynamodb_error_mapping(make_ctx, fake_boto3, error, expected) -> None:
    adapter = DynamoDBAdapter(make_ctx("aws_dynamodb", ProviderKind.DB, settings={"region": "eu-west-1"},
                              secrets=SECRETS))
    fake_boto3.fail = error
    with pytest.raises(expected):
        await adapter.list_tables()


async def test_dynamodb_not_found_and_missing_sdk(make_ctx, fake_boto3, monkeypatch) -> None:
    adapter = DynamoDBAdapter(make_ctx("aws_dynamodb", ProviderKind.DB, settings={"region": "eu-west-1"},
                              secrets=SECRETS))
    fake_boto3.fail = FakeClientError("ResourceNotFoundException", 400)
    with pytest.raises(ProviderError) as exc:
        await adapter.describe("ghost")
    assert exc.value.status_code == 404
    with pytest.raises(NotConfigured):
        await DynamoDBAdapter(make_ctx("aws_dynamodb", ProviderKind.DB, settings={"region": "r"})).list_tables()
    monkeypatch.setitem(sys.modules, "boto3", None)
    missing = DynamoDBAdapter(make_ctx("aws_dynamodb", ProviderKind.DB, settings={"region": "r"}, secrets=SECRETS))
    with pytest.raises(NotConfigured, match="pip install boto3"):
        await missing.list_tables()
