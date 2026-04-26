from .auth import router as auth_router
from .projects import router as projects_router
from .innovations import router as innovations_router
from .health import router as health_router

__all__ = [
    "auth_router",
    "projects_router",
    "innovations_router",
    "health_router",
]
