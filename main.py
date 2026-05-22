import base64
import io
import logging
import os
import re
import secrets
from typing import Optional
from urllib.parse import urlparse

import boto3
import qrcode
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from mangum import Mangum
from PIL import Image
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger()
logger.setLevel(logging.INFO)

Image.MAX_IMAGE_PIXELS = 10_000_000  # decompression bomb guard (~3162×3162px)

HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

TABLE_NAME = os.environ.get("TABLE_NAME", "jawsqr-urls")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = {"http", "https"}
LOGO_PATH = ""
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
MAX_IMAGE_SIZE = 2 * 1024 * 1024  # 2MB

dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "ap-northeast-1"))
table = dynamodb.Table(TABLE_NAME)

app = FastAPI()
templates = Jinja2Templates(directory="templates")


def _validate_color(color: str) -> str:
    if not HEX_COLOR_RE.match(color):
        raise HTTPException(status_code=400, detail="無効な色形式です（例: #000000）")
    return color


def _draw_round_dots(qr_obj, fill_color: str, back_color: str) -> Image.Image:
    from PIL import ImageDraw
    matrix = qr_obj.get_matrix()
    box = qr_obj.box_size
    bd = qr_obj.border
    n = len(matrix)
    size = (n + bd * 2) * box
    img = Image.new("RGB", (size, size), back_color)
    draw = ImageDraw.Draw(img)
    pad = 1
    for r, row in enumerate(matrix):
        for c, val in enumerate(row):
            if val:
                x0 = (c + bd) * box + pad
                y0 = (r + bd) * box + pad
                x1 = (c + bd + 1) * box - pad - 1
                y1 = (r + bd + 1) * box - pad - 1
                draw.ellipse([x0, y0, x1, y1], fill=fill_color)
    return img


def _is_valid_image(data: bytes) -> bool:
    if data[:4] == b"\x89PNG":
        return True
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"script-src 'nonce-{nonce}'; "
            f"style-src 'nonce-{nonce}'; "
            "img-src 'self' data:;"
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)


def validate_url(url: str) -> str:
    if len(url) > MAX_URL_LENGTH:
        raise HTTPException(status_code=400, detail=f"URLが長すぎます（最大 {MAX_URL_LENGTH} 文字）")

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)

    if parsed.scheme not in ALLOWED_SCHEMES:
        raise HTTPException(status_code=400, detail="http または https のURLのみ使用できます")

    # ホスト名が存在し、有効な形式か確認
    if not parsed.netloc or not re.match(r"^[a-zA-Z0-9.\-]+(:[0-9]+)?$", parsed.netloc):
        raise HTTPException(status_code=400, detail="無効なURLです")

    return url


def generate_qr_base64(
    url: str,
    logo_data: Optional[bytes] = None,
    fill_color: str = "#000000",
    back_color: str = "#ffffff",
    dot_style: str = "square",
) -> str:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    if dot_style == "round":
        qr_img = _draw_round_dots(qr, fill_color, back_color).convert("RGBA")
    else:
        qr_img = qr.make_image(fill_color=fill_color, back_color=back_color).convert("RGBA")
    qr_size = qr_img.size[0]

    logo_source = None
    if logo_data:
        logo_source = io.BytesIO(logo_data)
    elif os.path.exists(LOGO_PATH):
        logo_source = LOGO_PATH

    if logo_source:
        try:
            logo = Image.open(logo_source).convert("RGBA")
        except Exception:
            raise HTTPException(status_code=400, detail="画像の処理に失敗しました")

        embed_size = qr_size // 4
        ratio = min(embed_size / logo.width, embed_size / logo.height)
        new_w = int(logo.width * ratio)
        new_h = int(logo.height * ratio)
        logo = logo.resize((new_w, new_h), Image.NEAREST)

        pad = 8
        bg = Image.new("RGBA", (new_w + pad * 2, new_h + pad * 2), (255, 255, 255, 255))
        bg_x = (qr_size - bg.width) // 2
        bg_y = (qr_size - bg.height) // 2
        qr_img.paste(bg, (bg_x, bg_y))

        logo_x = (qr_size - new_w) // 2
        logo_y = (qr_size - new_h) // 2
        qr_img.paste(logo, (logo_x, logo_y), logo)

    buf = io.BytesIO()
    qr_img.convert("RGB").save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def create_short_code(length: int = 6) -> str:
    return secrets.token_urlsafe(length)[:length]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/shorten")
async def shorten(
    request: Request,
    url: str = Form(...),
    logo: Optional[UploadFile] = File(None),
    fill_color: str = Form("#000000"),
    back_color: str = Form("#ffffff"),
    dot_style: str = Form("square"),
):
    url = validate_url(url)
    fill_color = _validate_color(fill_color)
    back_color = _validate_color(back_color)
    if dot_style not in ("square", "round"):
        dot_style = "square"

    logo_data = None
    if logo and logo.filename:
        if logo.content_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(status_code=400, detail="画像ファイル（PNG、JPEG、GIF、WebP）のみ使用できます")
        content = await logo.read(MAX_IMAGE_SIZE + 1)
        if len(content) > MAX_IMAGE_SIZE:
            raise HTTPException(status_code=400, detail="画像サイズは2MB以内にしてください")
        if not _is_valid_image(content):
            raise HTTPException(status_code=400, detail="無効な画像ファイルです")
        logo_data = content

    res = table.query(
        IndexName="original_url-index",
        KeyConditionExpression=boto3.dynamodb.conditions.Key("original_url").eq(url),
        Limit=1,
    )
    if res["Items"]:
        code = res["Items"][0]["code"]
    else:
        code = create_short_code()
        table.put_item(Item={"code": code, "original_url": url})

    short_url = f"{BASE_URL.rstrip('/')}/r/{code}"
    qr_data = generate_qr_base64(short_url, logo_data, fill_color, back_color, dot_style)
    return {"short_url": short_url, "qr_base64": qr_data, "original_url": url}


@app.get("/r/{code}")
async def redirect_url(code: str):
    if not re.match(r"^[a-zA-Z0-9_\-]{1,20}$", code):
        raise HTTPException(status_code=400, detail="無効なコードです")

    res = table.get_item(Key={"code": code})
    item = res.get("Item")
    if not item:
        raise HTTPException(status_code=404, detail="短縮URLが見つかりません")

    target = item["original_url"]
    parsed = urlparse(target)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise HTTPException(status_code=400, detail="無効なリダイレクト先です")

    return RedirectResponse(url=target, status_code=302)


_base_path = urlparse(BASE_URL).path.rstrip("/") or "/"
handler = Mangum(app, api_gateway_base_path=_base_path)
