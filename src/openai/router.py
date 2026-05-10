"""OpenAI API router — mounts all /v1/* routes."""

from fastapi import APIRouter

from src.openai.chat import router as chat_router
from src.openai.models import router as models_router
from src.openai.passthrough import router as passthrough_router

router = APIRouter(prefix="/v1")
router.include_router(chat_router)
router.include_router(models_router)
router.include_router(passthrough_router)
