"""
Reusable case-insensitive search for any SQLModel/SQLAlchemy listing endpoint.

Independent of pagination_service and sorting_service — apply this FIRST
(before sort/paginate), since it narrows the row set those two then operate on.

Usage:
    from app.common.search_service import apply_search, search_query

    @router.get("", response_model=List[LaptopRead])
    def list_laptops(
        search: Optional[str] = search_query("Matches product name or model code"),
        session: Session = Depends(get_session),
    ):
        statement = select(Laptop)
        statement = apply_search(statement, search, [Laptop.product_name, Laptop.model_code])
        return session.exec(statement).all()
"""
from typing import Optional, Sequence

from fastapi import Query
from sqlalchemy import or_
from sqlalchemy.orm.attributes import InstrumentedAttribute


def search_query(description: str = "Case-insensitive search term") -> Optional[str]:
    """Standard `search` query param — use so every listing endpoint documents/behaves the same."""
    return Query(default=None, min_length=1, description=description)


def apply_search(statement, search: Optional[str], columns: Sequence[InstrumentedAttribute]):
    """
    Adds a `WHERE col1 ILIKE %term% OR col2 ILIKE %term% OR ...` clause.

    No-op (returns `statement` unchanged) when `search` is empty/None, or when
    `columns` is empty — callers don't need to guard the call themselves.
    """
    if not search or not columns:
        return statement

    pattern = f"%{search}%"
    return statement.where(or_(*[column.ilike(pattern) for column in columns]))
