"""
Reusable pagination for any SQLModel/SQLAlchemy listing endpoint.

Independent of search_service and sorting_service — apply this LAST, after any
WHERE (search) and ORDER BY (sort) clauses, so the total count and the
paginated slice both reflect the filtered result set.

Usage:
    from app.common.pagination_service import Page, PaginationParams, count_total, paginate

    @router.get("", response_model=Page[LaptopRead])
    def list_laptops(
        pagination: PaginationParams = Depends(),
        session: Session = Depends(get_session),
    ):
        statement = select(Laptop)
        total = count_total(session, statement)
        items = session.exec(paginate(statement, pagination)).all()
        return Page(items=list(items), total=total, skip=pagination.skip, limit=pagination.limit)
"""
from typing import Generic, List, TypeVar

from fastapi import Query
from sqlalchemy import func
from sqlmodel import Session, SQLModel, select

T = TypeVar("T")

DEFAULT_LIMIT = 50
# Generous ceiling: existing admin bulk-fetch call sites already request up to
# 500-1000 rows (benchmarks, raw-scrap queue) with no prior cap. This still
# rejects genuinely unbounded requests without breaking those.
MAX_LIMIT = 1000


class PaginationParams:
    """FastAPI dependency: add `pagination: PaginationParams = Depends()` to any route."""

    def __init__(
        self,
        skip: int = Query(default=0, ge=0, description="Number of rows to skip"),
        limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Max rows to return"),
    ):
        self.skip = skip
        self.limit = limit


def count_total(session: Session, statement) -> int:
    """
    Counts how many rows `statement` would return.

    Call this BEFORE paginate() — counting a statement that already has
    offset/limit applied just returns min(limit, remaining rows), not the
    true total the frontend needs for "X of Y" / page-count math.
    """
    return session.exec(select(func.count()).select_from(statement.subquery())).one()


def paginate(statement, pagination: PaginationParams):
    """Applies skip/limit to a statement. Call this last, after search/sort."""
    return statement.offset(pagination.skip).limit(pagination.limit)


class Page(SQLModel, Generic[T]):
    """Generic paginated response envelope: `Page[LaptopRead]`, `Page[UserAdminRead]`, ..."""

    items: List[T]
    total: int
    skip: int
    limit: int
