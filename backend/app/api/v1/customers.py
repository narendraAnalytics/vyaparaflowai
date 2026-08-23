import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_perm
from app.core.exceptions import ConflictError, NotFoundError
from app.db.models.org import User
from app.db.models.partners import Customer
from app.db.session import get_db
from app.schemas.master_data import CustomerCreate, CustomerOut, CustomerUpdate, Page

router = APIRouter(prefix="/customers", tags=["customers"])


async def _get_org_customer(
    db: AsyncSession, org_id: uuid.UUID, customer_id: uuid.UUID
) -> Customer:
    customer = await db.get(Customer, customer_id)
    if customer is None or customer.org_id != org_id:
        raise NotFoundError(f"customer {customer_id} not found")
    return customer


@router.get("", response_model=Page[CustomerOut])
async def list_customers(
    user: User = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    q: str | None = Query(default=None, description="Filter by name, case-insensitive substring"),
    is_active: bool | None = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[CustomerOut]:
    stmt = select(Customer).where(Customer.org_id == user.org_id)
    count_stmt = select(func.count()).select_from(Customer).where(Customer.org_id == user.org_id)
    if q:
        stmt = stmt.where(Customer.name.ilike(f"%{q}%"))
        count_stmt = count_stmt.where(Customer.name.ilike(f"%{q}%"))
    if is_active is not None:
        stmt = stmt.where(Customer.is_active == is_active)
        count_stmt = count_stmt.where(Customer.is_active == is_active)

    total = (await db.execute(count_stmt)).scalar_one()
    rows = (
        (await db.execute(stmt.order_by(Customer.name).limit(limit).offset(offset))).scalars().all()
    )
    return Page(items=list(rows), total=total, limit=limit, offset=offset)


@router.get("/{customer_id}", response_model=CustomerOut)
async def get_customer(
    customer_id: uuid.UUID,
    user: User = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Customer:
    return await _get_org_customer(db, user.org_id, customer_id)


@router.post("", response_model=CustomerOut, status_code=201)
async def create_customer(
    payload: CustomerCreate,
    user: User = Depends(require_perm("customer.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Customer:
    customer = Customer(org_id=user.org_id, **payload.model_dump())
    db.add(customer)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("customer violates a database constraint (check GSTIN format)") from exc
    await db.refresh(customer)
    return customer


@router.patch("/{customer_id}", response_model=CustomerOut)
async def update_customer(
    customer_id: uuid.UUID,
    payload: CustomerUpdate,
    user: User = Depends(require_perm("customer.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Customer:
    customer = await _get_org_customer(db, user.org_id, customer_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(customer, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("customer violates a database constraint (check GSTIN format)") from exc
    await db.refresh(customer)
    return customer


@router.delete("/{customer_id}", status_code=204)
async def deactivate_customer(
    customer_id: uuid.UUID,
    user: User = Depends(require_perm("customer.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> None:
    customer = await _get_org_customer(db, user.org_id, customer_id)
    customer.is_active = False
    await db.commit()
    return None
