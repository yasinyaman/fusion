"""Request bodies for the REST API."""

from pydantic import BaseModel


class SQLRequest(BaseModel):
    sql: str


class SearchRequest(BaseModel):
    table: str
    filter_column: str
    filter_value: str
    limit: int = 20


class AggregateRequest(BaseModel):
    table: str
    group_by: str
    agg_column: str
    agg_func: str


class CreateViewRequest(BaseModel):
    name: str
    sql: str
    refresh: str = "manual"
