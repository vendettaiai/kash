"""Custom merchant logos: upload, serve, remove (services/merchant_icons.py)."""

from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse, Response

from kash.domain.models import MerchantIcon
from kash.services import merchant_icons
from kash.web.deps import OWNER_USER_ID, Provider

router = APIRouter()


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


@router.post("/merchant-icons")
async def upload_merchant_icon(
    provider: Provider,
    merchant: Annotated[str, Form()],
    file: Annotated[UploadFile, File()],
) -> JSONResponse:
    """Set (or replace) the logo for a merchant. PNG, JPEG, WebP or GIF, up to 512 KB."""
    key = merchant_icons.merchant_key(merchant)
    if not key:
        return _error("This bill has no merchant name to attach an icon to.")
    data = await file.read(merchant_icons.MAX_UPLOAD_BYTES + 1)
    if len(data) > merchant_icons.MAX_UPLOAD_BYTES:
        return _error("The image is too large (at most 512 KB).")
    content_type = merchant_icons.sniff(data)
    if content_type is None:
        return _error("Use a PNG, JPEG, WebP or GIF image.")
    await provider.merchant_icons.save_icon(
        MerchantIcon(
            id=uuid4(), user_id=OWNER_USER_ID, merchant=key, content_type=content_type, data=data
        )
    )
    await merchant_icons.load_custom(provider, OWNER_USER_ID)
    return JSONResponse({"ok": True, "merchant": key})


@router.get("/merchant-icons/{icon_id}")
async def merchant_icon_image(icon_id: UUID, provider: Provider) -> Response:
    icon = await provider.merchant_icons.get_icon(icon_id)
    if icon is None or icon.user_id != OWNER_USER_ID:
        return Response(status_code=404)
    return Response(
        icon.data,
        media_type=icon.content_type,
        headers={
            # The URL carries ?v=<updated>, so a new upload gets a new URL.
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


@router.post("/merchant-icons/{icon_id}/delete")
async def delete_merchant_icon(icon_id: UUID, provider: Provider) -> JSONResponse:
    icon = await provider.merchant_icons.get_icon(icon_id)
    if icon is None or icon.user_id != OWNER_USER_ID:
        return _error("That icon no longer exists.", 404)
    await provider.merchant_icons.delete_icon(icon_id)
    await merchant_icons.load_custom(provider, OWNER_USER_ID)
    return JSONResponse({"ok": True})
