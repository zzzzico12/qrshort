import base64
import io
import logging
import os
import re
import secrets
from urllib.parse import urlparse

import boto3
import qrcode
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from mangum import Mangum
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger()
logger.setLevel(logging.INFO)

TABLE_NAME = os.environ.get("TABLE_NAME", "jawsqr-urls")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
MAX_URL_LENGTH = 2048
ALLOWED_SCHEMES = {"http", "https"}

dynamodb = boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "ap-northeast-1"))
table = dynamodb.Table(TABLE_NAME)

app = FastAPI()
templates = Jinja2Templates(directory="templates")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
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


def generate_qr_base64(url: str) -> str:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def create_short_code(length: int = 6) -> str:
    return secrets.token_urlsafe(length)[:length]


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/shorten")
async def shorten(request: Request, url: str = Form(...)):
    url = validate_url(url)

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
    qr_data = generate_qr_base64(short_url)
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


handler = Mangum(app)
