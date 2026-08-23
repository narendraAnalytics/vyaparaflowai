import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_perm
from app.core.exceptions import ConflictError, NotFoundError
from app.db.models.catalog import Warehouse
from app.db.models.org import User
from app.db.session import get_db
from app.schemas.master_data import Page, WarehouseCreate, WarehouseOut, WarehouseUpdate

router = APIRouter(prefix="/warehouses", tags=["warehouses"])


async def _get_org_warehouse(
    db: AsyncSession, org_id: uuid.UUID, warehouse_id: uuid.UUID
) -> Warehouse:
    warehouse = await db.get(Warehouse, warehouse_id)
    if warehouse is None or warehouse.org_id != org_id:
        raise NotFoundError(f"warehouse {warehouse_id} not found")
    return warehouse


@router.get("", response_model=Page[WarehouseOut])
async def list_warehouses(
    user: User = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    q: str | None = Query(
        default=None, description="Filter by code or name, case-insensitive substring"
    ),
    is_active: bool | None = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[WarehouseOut]:
    stmt = select(Warehouse).where(Warehouse.org_id == user.org_id)
    count_stmt = select(func.count()).select_from(Warehouse).where(Warehouse.org_id == user.org_id)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where((Warehouse.code.ilike(pattern)) | (Warehouse.name.ilike(pattern)))
        count_stmt = count_stmt.where(
            (Warehouse.code.ilike(pattern)) | (Warehouse.name.ilike(pattern))
        )
    if is_active is not None:
        stmt = stmt.where(Warehouse.is_active == is_active)
        count_stmt = count_stmt.where(Warehouse.is_active == is_active)

    total = (await db.execute(count_stmt)).scalar_one()
    rows = (
        (await db.execute(stmt.order_by(Warehouse.code).limit(limit).offset(offset)))
        .scalars()
        .all()
    )
    return Page(items=list(rows), total=total, limit=limit, offset=offset)


@router.get("/{warehouse_id}", response_model=WarehouseOut)
async def get_warehouse(
    warehouse_id: uuid.UUID,
    user: User = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Warehouse:
    return await _get_org_warehouse(db, user.org_id, warehouse_id)


@router.post("", response_model=WarehouseOut, status_code=201)
async def create_warehouse(
    payload: WarehouseCreate,
    user: User = Depends(require_perm("warehouse.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Warehouse:
    warehouse = Warehouse(org_id=user.org_id, **payload.model_dump())
    db.add(warehouse)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(f"warehouse with code {payload.code!r} already exists") from exc
    await db.refresh(warehouse)
    return warehouse


@router.patch("/{warehouse_id}", response_model=WarehouseOut)
async def update_warehouse(
    warehouse_id: uuid.UUID,
    payload: WarehouseUpdate,
    user: User = Depends(require_perm("warehouse.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Warehouse:
    warehouse = await _get_org_warehouse(db, user.org_id, warehouse_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(warehouse, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("warehouse violates a database constraint (duplicate code?)") from exc
    await db.refresh(warehouse)
    return warehouse


@router.delete("/{warehouse_id}", status_code=204)
async def deactivate_warehouse(
    warehouse_id: uuid.UUID,
    user: User = Depends(require_perm("warehouse.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> None:
    warehouse = await _get_org_warehouse(db, user.org_id, warehouse_id)
    warehouse.is_active = False
    await db.commit()
    return None
