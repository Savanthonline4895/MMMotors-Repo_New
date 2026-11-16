"""API routes: files (split out of server.py; code moved verbatim)."""
from typing import Optional
from fastapi import Depends, HTTPException, Query, UploadFile, File, Response
from fastapi.responses import StreamingResponse
import os
import io
from database import verify_token, obj_id
import io
from fastapi import File, UploadFile
from fastapi.responses import StreamingResponse
from routers import api_router

# Set by server.py's lifespan (and by tests/conftest.py) once Mongo is connected.
db = None
fs = None

# ═══════════════════════════════════════════════════════════════════════════════
#  FILES (GridFS Uploads)
# ═══════════════════════════════════════════════════════════════════════════════

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
# The stored type comes from the file's own bytes, never from the client's Content-Type.
_UPLOAD_SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF-", "application/pdf"),
)
_INLINE_TYPES = {"image/jpeg", "image/png", "image/webp"}

def _sniff_upload_type(head: bytes) -> Optional[str]:
    for magic, ctype in _UPLOAD_SIGNATURES:
        if head.startswith(magic):
            return ctype
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None

@api_router.post("/upload")
async def upload_file(file: UploadFile = File(...), current_user=Depends(verify_token)):
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    content_type = _sniff_upload_type(data[:16])
    if not content_type:
        raise HTTPException(status_code=415, detail="Only JPEG, PNG, WebP or PDF files can be uploaded")
    filename = os.path.basename((file.filename or "upload").replace("\\", "/"))[:200] or "upload"
    file_id = await fs.upload_from_stream(
        filename,
        io.BytesIO(data),
        metadata={"content_type": content_type, "uploaded_by": current_user["username"]}
    )
    return {"file_id": str(file_id), "filename": filename}

@api_router.get("/qr.svg")
async def upi_qr_svg(data: str = Query(..., max_length=400)):
    """Public. Draws the scan-to-pay QR printed on bills. Only UPI payment links are
    accepted, so this can't be used as a general-purpose QR service."""
    if not data.startswith("upi://pay?"):
        raise HTTPException(status_code=400, detail="Only UPI payment links")
    from reportlab.graphics.barcode.qr import QrCodeWidget
    from reportlab.graphics.shapes import Drawing
    from reportlab.graphics import renderSVG
    qr = QrCodeWidget(data, barLevel="M")   # M: still scans if the print is a little smudged
    x0, y0, x1, y1 = qr.getBounds()
    drawing = Drawing(x1 - x0, y1 - y0)
    drawing.add(qr)
    return Response(content=renderSVG.drawToString(drawing), media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})

@api_router.get("/files/{file_id}")
async def get_file(file_id: str, current_user=Depends(verify_token)):
    try:
        grid_out = await fs.open_download_stream(obj_id(file_id))
        async def file_stream():
            while chunk := await grid_out.readchunk():
                yield chunk
        stored = (grid_out.metadata or {}).get("content_type") or ""
        inline = stored in _INLINE_TYPES
        return StreamingResponse(
            file_stream(),
            media_type=stored if inline or stored == "application/pdf" else "application/octet-stream",
            headers={
                # Never let a stored file run as a page on this origin
                "Content-Disposition": "inline" if inline else "attachment",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Cache-Control": "private, no-store",
            },
        )
    except Exception:
        raise HTTPException(status_code=404, detail="File not found")
