"""The two API routers every route module registers on."""
from fastapi import APIRouter

api_router = APIRouter(prefix="/api")

import_router = APIRouter(prefix="/api/import", tags=["import"])
