import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user, require_perm
from app.core.exceptions import ConflictError, NotFoundError
from app.db.models.catalog import Product
from app.db.models.org import User
from app.db.session import get_db
from app.schemas.master_data import Page, ProductCreate, ProductOut, ProductUpdate

router = APIRouter(prefix="/products", tags=["products"])


async def _get_org_product(db: AsyncSession, org_id: uuid.UUID, product_id: uuid.UUID) -> Product:
    product = await db.get(Product, product_id)
    if product is None or product.org_id != org_id:
        raise NotFoundError(f"product {product_id} not found")
    return product


@router.get("", response_model=Page[ProductOut])
async def list_products(
    user: User = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
    q: str | None = Query(
        default=None, description="Filter by SKU or name, case-insensitive substring"
    ),
    category: str | None = Query(default=None),
    is_active: bool | None = Query(default=True),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> Page[ProductOut]:
    stmt = select(Product).where(Product.org_id == user.org_id)
    count_stmt = select(func.count()).select_from(Product).where(Product.org_id == user.org_id)
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where((Product.sku.ilike(pattern)) | (Product.name.ilike(pattern)))
        count_stmt = count_stmt.where((Product.sku.ilike(pattern)) | (Product.name.ilike(pattern)))
    if category:
        stmt = stmt.where(Product.category == category)
        count_stmt = count_stmt.where(Product.category == category)
    if is_active is not None:
        stmt = stmt.where(Product.is_active == is_active)
        count_stmt = count_stmt.where(Product.is_active == is_active)

    total = (await db.execute(count_stmt)).scalar_one()
    rows = (
        (await db.execute(stmt.order_by(Product.sku).limit(limit).offset(offset))).scalars().all()
    )
    return Page(items=list(rows), total=total, limit=limit, offset=offset)


@router.get("/{product_id}", response_model=ProductOut)
async def get_product(
    product_id: uuid.UUID,
    user: User = Depends(get_current_user),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Product:
    return await _get_org_product(db, user.org_id, product_id)


@router.post("", response_model=ProductOut, status_code=201)
async def create_product(
    payload: ProductCreate,
    user: User = Depends(require_perm("product.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Product:
    product = Product(org_id=user.org_id, **payload.model_dump())
    db.add(product)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError(
            f"product with SKU {payload.sku!r} already exists, or violates a constraint"
        ) from exc
    await db.refresh(product)
    return product


@router.patch("/{product_id}", response_model=ProductOut)
async def update_product(
    product_id: uuid.UUID,
    payload: ProductUpdate,
    user: User = Depends(require_perm("product.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> Product:
    product = await _get_org_product(db, user.org_id, product_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(product, field, value)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("product violates a database constraint (duplicate SKU?)") from exc
    await db.refresh(product)
    return product


@router.delete("/{product_id}", status_code=204)
async def deactivate_product(
    product_id: uuid.UUID,
    user: User = Depends(require_perm("product.manage")),  # noqa: B008
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> None:
    product = await _get_org_product(db, user.org_id, product_id)
    product.is_active = False
    await db.commit()
    return None
