"""Simple navigation pages."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from kash.web.deps import OWNER_USER_ID, Provider, get_templates

router = APIRouter()


@router.get("/more", response_class=HTMLResponse)
async def more(request: Request, provider: Provider) -> HTMLResponse:
    """Mobile navigation hub."""
    saved = await provider.settings.get_settings(OWNER_USER_ID)
    return get_templates(request).TemplateResponse(
        request=request,
        name="pages/more.html",
        context={"active_page": "more", "ai_enabled": saved.ai_enabled},
    )
