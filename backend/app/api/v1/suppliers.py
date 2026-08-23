import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_perm
from app.core.exceptions import ConflictError, NotFoundError
from app.db.models.org import User
from app.db.models.partners import Supplier
from app.db.session import get_db
from app.schemas.master_data import Page, SupplierCreate, SupplierOut, SupplierUpdate

router = APIRouter(prefix="/suppliers", tags=["suppliers"])


async def _get_org_supplier(
    db: AsyncSession, org_id: uuid.UUID, supplier_id: uuid.UUID
) -> Supplier:
    supplier = await db.get(Supplier, supplier_id)
    if supplier is None or supplier.org_id != org_id:
        raise NotFoundError(f"supplier {supplier_id} not found")
    return supplier


@router.get("", response_model=Page[SupplierOut])
async def list_suppliers(
    user: User = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    q: str | None = Query(default=None, description="Filter by name, case-insensitive substring"),
    is_active: bool | None = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[SupplierOut]:
    stmt = select(Supplier).where(Supplier.org_id == user.org_id)
    count_stmt = select(func.count()).select_from(Supplier).where(Supplier.org_id == user.org_id)
    if q:
        stmt = stmt.where(Supplier.name.ilike(f"%{q}%"))
        count_stmt = count_stmt.where(Supplier.name.ilike(f"%{q}%"))
    if is_active is not None:
        stmt = stmt.where(Supplier.is_active == is_active)
        count_stmt = count_stmt.where(Supplier.is_active == is_active)

    total = (await db.execute(count_stmt)).scalar_one()
    rows = (
        (await db.execute(stmt.order_by(Supplier.name).limit(limit).offset(offset))).scalars().all()
    )
    return Page(items=list(rows), total=total, limit=limit, offset=offset)


@router.get("/{supplier_id}", response_model=SupplierOut)
async def get_supplier(
    supplier_id: uuid.UUID,
    user: User = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Supplier:
    return await _get_org_supplier(db, user.org_id, supplier_id)


@router.post("", response_model=SupplierOut, status_code=201)
async def create_supplier(
    payload: SupplierCreate,
    user: User = Depends(require_perm("supplier.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Supplier:
    supplier = Supplier(org_id=user.org_id, **payload.model_dump())
    db.add(supplier)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("supplier violates a database constraint (check GSTIN format)") from exc
    await db.refresh(supplier)
    return supplier


@router.patch("/{supplier_id}", response_model=SupplierOut)
async def update_supplier(
    supplier_id: uuid.UUID,
    payload: SupplierUpdate,
    user: User = Depends(require_perm("supplier.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Supplier:
    supplier = await _get_org_supplier(db, user.org_id, supplier_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(supplier, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("supplier violates a database constraint (check GSTIN format)") from exc
    await db.refresh(supplier)
    return supplier


@router.delete("/{supplier_id}", status_code=204)
async def deactivate_supplier(
    supplier_id: uuid.UUID,
    user: User = Depends(require_perm("supplier.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> None:
    supplier = await _get_org_supplier(db, user.org_id, supplier_id)
    supplier.is_active = False
    await db.commit()
    return None
