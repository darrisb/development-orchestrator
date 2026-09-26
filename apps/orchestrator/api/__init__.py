from fastapi import APIRouter

from .errors import register_exception_handlers
from .experience import router as experience_router
from .health import router as health_router
from .models import router as models_router
from .projects import router as projects_router
from .reviews import router as reviews_router
from .tasks import router as tasks_router

api_router = APIRouter()
api_router.include_router(experience_router)
api_router.include_router(health_router)
api_router.include_router(models_router)
api_router.include_router(projects_router)
api_router.include_router(reviews_router)
api_router.include_router(tasks_router)

__all__ = ["api_router", "register_exception_handlers"]
