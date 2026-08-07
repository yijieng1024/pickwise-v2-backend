"""
Reusable, allow-listed filtering for any SQLModel/SQLAlchemy listing endpoint.

Sits alongside search_service (both narrow the row set) and applies BEFORE
count_total() and sorting/pagination, so the total and the returned page both
reflect the filtered set. The full order for a listing route is:

    filter -> search -> count_total -> sort -> paginate

Each route defines its own `filterable_columns` map (field name -> column), the
same allow-list idea as sorting_service: a caller can never filter on a column
that isn't meant to be exposed, and a typo'd field gets a clear 400 rather than
being silently ignored.

Why this exists: every listing had grown its own `if value: statement =
statement.where(Col == value)` ladder. That is fine once and unreadable by the
fourth filter, it silently ignores an unknown field, and each copy has to
remember to filter *before* count_total or the "X of Y" total comes out wrong.

Usage:
    from app.common.filter_service import apply_filters, apply_range

    LAPTOP_FILTERABLE_COLUMNS = {
        "brand_id": Laptop.brand_id,
        "is_active": Laptop.is_active,
    }

    @router.get("", response_model=Page[LaptopRead])
    def list_laptops(
        brand_id: Optional[UUID] = Query(default=None),
        is_active: Optional[bool] = Query(default=None),
        price_min: Optional[float] = Query(default=None, ge=0),
        price_max: Optional[float] = Query(default=None, ge=0),
        pagination: PaginationParams = Depends(),
        session: Session = Depends(get_session),
    ):
        statement = select(Laptop)
        statement = apply_filters(
            statement,
            {"brand_id": brand_id, "is_active": is_active},
            LAPTOP_FILTERABLE_COLUMNS,
        )
        statement = apply_range(statement, Laptop.price_rm, price_min, price_max)
        ...
"""
from typing import Any, Dict, Mapping, Optional, Sequence

from fastapi import HTTPException, Query, status
from sqlalchemy.orm.attributes import InstrumentedAttribute


def filter_query(description: str, **kwargs) -> Any:
    """
    Standard optional-filter query param, so every listing documents filters
    the same way. Thin wrapper over `Query(default=None, ...)`; pass `alias=`
    when the public name differs from the Python argument (`status` vs
    `status_filter`, which shadows a FastAPI import).
    """
    return Query(default=None, description=description, **kwargs)


def apply_filters(
    statement,
    filters: Mapping[str, Any],
    filterable_columns: Dict[str, InstrumentedAttribute],
):
    """
    Adds one `WHERE column = value` per supplied filter, ANDed together.

    - `None` values are skipped, so a route can pass every filter it knows
      about and let the ones the caller omitted fall away. Callers never need
      to guard the call themselves.
    - A list/tuple/set value becomes `WHERE column IN (...)`, which is how a
      route exposes a repeated query param (`?status=queued&status=processing`).
      An *empty* sequence is treated as "not supplied" rather than as
      `IN ()` — the latter matches nothing and would silently blank a listing.
    - A key that isn't in `filterable_columns` raises 400, mirroring
      apply_sort. This catches a typo'd field name at the call site instead of
      quietly returning unfiltered rows.

    Note `False` and `0` are real values and ARE applied; only `None` means
    "not supplied". Guarding with a truthiness check here would make
    `?is_active=false` unfilterable, which is the bug this replaces.
    """
    for field, value in filters.items():
        if value is None:
            continue

        column = filterable_columns.get(field)
        if column is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Invalid filter '{field}'. "
                    f"Choose one of: {', '.join(sorted(filterable_columns))}"
                ),
            )

        if isinstance(value, (list, tuple, set, frozenset)):
            values = list(value)
            if not values:
                continue
            statement = statement.where(column.in_(values))
        else:
            statement = statement.where(column == value)

    return statement


def apply_range(
    statement,
    column: InstrumentedAttribute,
    minimum: Optional[Any] = None,
    maximum: Optional[Any] = None,
):
    """
    Adds `column >= minimum` and/or `column <= maximum`.

    Both bounds are optional and independent, so one-sided ranges ("under
    RM4000", "since last Tuesday") work without a sentinel. Inclusive at both
    ends, which is what a price or date filter in a UI is taken to mean.

    Deliberately not folded into apply_filters: a range is two comparisons on
    one column, so it does not fit the field -> value shape, and keeping it
    separate leaves the equality path simple.
    """
    if minimum is not None:
        statement = statement.where(column >= minimum)
    if maximum is not None:
        statement = statement.where(column <= maximum)
    return statement


def apply_in(
    statement,
    column: InstrumentedAttribute,
    values: Optional[Sequence[Any]],
):
    """
    `WHERE column IN (...)` for a set of values that isn't tied to a named
    filter field — an internal narrowing the route computes itself, such as
    "queued or processing" behind a single `active_only` flag.

    No-op on None or an empty sequence, for the same reason as apply_filters:
    an empty IN matches nothing and would blank the listing.
    """
    if not values:
        return statement
    return statement.where(column.in_(list(values)))
