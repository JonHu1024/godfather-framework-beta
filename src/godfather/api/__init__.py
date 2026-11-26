"""API 路由包。"""
from fastapi import APIRouter

from . import cordis_admin, events, pipeline, projects, training

api_router = APIRouter()
api_router.include_router(cordis_admin.router)
api_router.include_router(projects.router)
api_router.include_router(pipeline.router)
api_router.include_router(events.router)
api_router.include_router(training.router)

__all__ = ["api_router"]
