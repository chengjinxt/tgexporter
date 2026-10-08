from __future__ import annotations

from io import BytesIO


def verify_webp_support() -> dict[str, object]:
    """Exercise the WebP codec so packaged builds fail verification if its DLL is missing."""
    try:
        from PIL import Image, features
    except ImportError as exc:
        raise RuntimeError("Pillow is required for WebP image support.") from exc

    if not features.check("webp"):
        raise RuntimeError("The installed Pillow runtime was built without WebP support.")

    buffer = BytesIO()
    Image.new("RGB", (2, 2), (32, 96, 160)).save(buffer, format="WEBP", lossless=True)
    buffer.seek(0)
    with Image.open(buffer) as image:
        image.load()
        if image.format != "WEBP" or image.size != (2, 2):
            raise RuntimeError("Pillow WebP codec self-check returned an unexpected image.")

    return {"pillow_webp": True, "decoded_size": [2, 2]}
