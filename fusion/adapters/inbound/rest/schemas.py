"""Request bodies for the REST API."""

from pydantic import BaseModel, Field


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


class LoadTableRequest(BaseModel):
    """Optional body of ``POST /tables/{table}/load``.

    Without it the whole table is loaded (and refused when it is too big);
    with it, only the matching rows and columns are fetched.
    """

    where: str | None = Field(
        default=None,
        description=(
            "Filter pushed to the source: an AND of simple conditions "
            "comparing a column to a literal."
        ),
    )
    columns: list[str] | None = Field(
        default=None, description="Columns to fetch instead of all of them"
    )
