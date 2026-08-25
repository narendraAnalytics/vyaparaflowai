from fastapi import APIRouter

from app.api.v1.approvals import router as approvals_router
from app.api.v1.auth import router as auth_router
from app.api.v1.customers import router as customers_router
from app.api.v1.matching import router as matching_router
from app.api.v1.payments import router as payments_router
from app.api.v1.procurement import router as procurement_router
from app.api.v1.products import router as products_router
from app.api.v1.receiving import router as receiving_router
from app.api.v1.sales_orders import router as sales_orders_router
from app.api.v1.suppliers import router as suppliers_router
from app.api.v1.warehouses import router as warehouses_router
from app.api.v1.workflow_events import router as workflow_events_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth_router)
api_router.include_router(customers_router)
api_router.include_router(suppliers_router)
api_router.include_router(products_router)
api_router.include_router(warehouses_router)
api_router.include_router(sales_orders_router)
api_router.include_router(procurement_router)
api_router.include_router(receiving_router)
api_router.include_router(matching_router)
api_router.include_router(payments_router)
api_router.include_router(approvals_router)
api_router.include_router(workflow_events_router)
