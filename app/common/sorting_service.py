"""
Reusable, allow-listed sorting for any SQLModel/SQLAlchemy listing endpoint.

Independent of pagination_service and search_service — apply this AFTER
search and BEFORE paginate(), so the offset/limit window lands on rows in
the requested order.

Each route defines its own `sortable_columns` map (field name -> column) so
a caller can never sort by a column that isn't meant to be exposed, and gets
a clear 400 instead of a silent no-op or a 500 from an invalid attribute.

Usage:
    from app.common.sorting_service import SortDirection, apply_sort, sort_dir_query

    LAPTOP_SORTABLE_COLUMNS = {
        "product_name": Laptop.product_name,
        "price_rm": Laptop.price_rm,
        "created_at": Laptop.created_at,
    }

    @router.get("", response_model=List[LaptopRead])
    def list_laptops(
        sort_by: Optional[str] = Query(default=None, description="One of: product_name, price_rm, created_at"),
        sort_dir: SortDirection = sort_dir_query(),
        session: Session = Depends(get_session),
    ):
        statement = select(Laptop)
        statement = apply_sort(statement, sort_by, sort_dir, LAPTOP_SORTABLE_COLUMNS, Laptop.created_at)
        return session.exec(statement).all()
"""
from enum import Enum
from typing import Dict, Optional

from fastapi import HTTPException, Query, status
from sqlalchemy.orm.attributes import InstrumentedAttribute


class SortDirection(str, Enum):
    asc = "asc"
    desc = "desc"


def sort_dir_query(default: SortDirection = SortDirection.asc) -> SortDirection:
    """Standard `sort_dir` query param — use so every listing endpoint behaves the same."""
    return Query(default=default, description="asc or desc")


def apply_sort(
    statement,
    sort_by: Optional[str],
    sort_dir: SortDirection,
    sortable_columns: Dict[str, InstrumentedAttribute],
    default_column: InstrumentedAttribute,
    tiebreak: Optional[InstrumentedAttribute] = None,
):
    """
    Adds `ORDER BY <column> ASC|DESC` to a statement.

    `sort_by` must be a key of `sortable_columns` (the route's own allow-list)
    or omitted entirely; anything else raises 400 rather than sorting on an
    unintended/unindexed column or silently ignoring the request. When
    `sort_by` is omitted, falls back to `default_column` — still respecting
    `sort_dir`, so callers can flip a default-sorted list without naming it.

    `tiebreak` — pass the table's primary key on any route that paginates.
    None of the sortable columns are unique (dozens of laptops share a price,
    a name, a creation timestamp), and rows tied on the sort column fall
    through to whatever order Postgres returns, which it does not guarantee
    and which is free to differ between the OFFSET 0 query and the OFFSET 20
    one — so a tied row can appear on two pages or on neither. Appending a
    unique column makes the order total and the paging stable.
    """
    if sort_by is None:
        column = default_column
    else:
        column = sortable_columns.get(sort_by)
        if column is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid sort_by '{sort_by}'. Choose one of: {', '.join(sortable_columns)}",
            )

    statement = statement.order_by(
        column.desc() if sort_dir == SortDirection.desc else column.asc()
    )
    # Always ascending: the tiebreak exists to be deterministic, not to be
    # meaningful, and flipping it with sort_dir would only make it harder to
    # reason about which of two tied rows comes first.
    return statement.order_by(tiebreak.asc()) if tiebreak is not None else statement
