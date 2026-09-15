"""Private native-search and explicit Browser tool adapter. Features default off."""

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from web_capabilities.auth import AuthenticationError, Authenticator
from web_capabilities.browser import BrowserPolicyError
from web_capabilities.gateway import GatewayRateLimitError, GatewayTimeoutError


@dataclass(frozen=True)
class Settings:
    search_enabled: bool = False
    browser_enabled: bool = False
    identity_header: str = "X-OpenWebUI-User-Jwt"
    concurrency: int = 2
    requests_per_minute: int = 5
    max_subjects: int = 1024


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=200)
    count: int = Field(default=3, ge=1, le=25)


class BrowserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(min_length=1, max_length=2048)


class Admission:
    """Per-process admission, not distributed billing or a per-chat quota."""

    def __init__(self, settings: Settings):
        if not 1 <= settings.concurrency <= 4 or not 1 <= settings.requests_per_minute <= 10:
            raise ValueError("Unsupported work limits")
        self.settings = settings
        self.subjects = {}
        self.active = set()

    @asynccontextmanager
    async def enter(self, subject: str):
        now = time.monotonic()
        self.subjects = {key: value for key, value in self.subjects.items() if now - value[0] < 60}
        if subject in self.active or len(self.active) >= self.settings.concurrency:
            raise HTTPException(429, "Web capability capacity exhausted")
        if subject not in self.subjects and len(self.subjects) >= self.settings.max_subjects:
            raise HTTPException(429, "Web capability capacity exhausted")
        started, count = self.subjects.get(subject, (now, 0))
        if count >= self.settings.requests_per_minute:
            raise HTTPException(429, "Web capability request limit reached")
        self.subjects[subject] = (started, count + 1)
        self.active.add(subject)
        try:
            yield
        finally:
            self.active.remove(subject)


async def read_request(request: Request, model):
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > 8192:
            raise HTTPException(413, "Request too large")
        data.extend(chunk)
    try:
        return model.model_validate_json(bytes(data))
    except ValidationError:
        raise HTTPException(422, "Invalid web capability request") from None


def create_app(auth: Authenticator, search_client=None, browser_loader=None, settings=None):
    settings = settings or Settings()
    admission = Admission(settings)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def identity(request: Request):
        try:
            return auth.authenticate(
                request.headers.get("Authorization", ""),
                request.headers.get(settings.identity_header, ""),
            )
        except AuthenticationError:
            raise HTTPException(401, "Authentication required") from None

    @app.get("/health")
    async def health():
        return {"status": "ok", "search_enabled": settings.search_enabled, "browser_enabled": settings.browser_enabled}

    @app.post("/search")
    async def search(request: Request):
        principal = identity(request)
        if not settings.search_enabled or search_client is None:
            raise HTTPException(503, "Managed search is disabled")
        body = await read_request(request, SearchRequest)
        async with admission.enter(principal.subject):
            try:
                result = await asyncio.wait_for(search_client.search(body.query, body.count), timeout=25)
            except (TimeoutError, GatewayTimeoutError):
                raise HTTPException(504, "Managed search deadline exceeded") from None
            except GatewayRateLimitError:
                raise HTTPException(429, "Managed search rate limit exceeded") from None
            except Exception:
                raise HTTPException(502, "Managed search failed") from None
        if result.metadata.get("requires_attribution_review"):
            raise HTTPException(502, "Managed search attribution requires review")
        return [{"link": record.link, "title": record.title, "snippet": record.snippet} for record in result.records[:body.count]]

    @app.post("/browser")
    async def browser(request: Request):
        principal = identity(request)
        if not settings.browser_enabled or browser_loader is None:
            raise HTTPException(503, "Browser loading is disabled")
        body = await read_request(request, BrowserRequest)
        async with admission.enter(principal.subject):
            try:
                return await asyncio.wait_for(browser_loader.load(body.url), timeout=55)
            except TimeoutError:
                raise HTTPException(504, "Browser loading deadline exceeded") from None
            except (ValueError, BrowserPolicyError):
                raise HTTPException(400, "URL rejected by Browser policy") from None
            except Exception:
                raise HTTPException(502, "Browser loading failed") from None

    return app
