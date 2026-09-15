"""
title: AgentCore public Browser reader (experimental)
description: Explicit identity-aware rendered-page reading; not the native fetch_url loader.
version: 0.1.0
"""

import json
import os
from types import SimpleNamespace
from urllib.parse import urlsplit

import aiohttp


class Tools:
    def __init__(self):
        self.citation = False

    async def browser_fetch_url(self, url: str, __user__: dict = None, __event_emitter__=None) -> str:
        """Read a public HTML page with AgentCore Browser, when explicitly enabled.

        Use for approved JavaScript-rendered public pages. No authenticated pages,
        clicks, forms, uploads, downloads or access-control bypass are supported.
        """
        from open_webui.env import FORWARD_USER_INFO_HEADER_JWT
        from open_webui.utils.headers import include_user_info_headers

        if not __user__ or __user__.get("role") not in ("user", "admin"):
            return json.dumps({"error": "A signed-in authorized user is required"})
        base = os.environ.get("AGENTCORE_WEB_ADAPTER_URL", "http://127.0.0.1:8090").rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port != 8090 or parsed.path or parsed.query or parsed.fragment or parsed.username:
            return json.dumps({"error": "Browser adapter configuration is invalid"})
        service_key = os.environ.get("EXTERNAL_WEB_SEARCH_API_KEY", "")
        if len(service_key.encode()) < 32:
            return json.dumps({"error": "Browser adapter authentication is not configured"})
        user = SimpleNamespace(**{key: __user__.get(key, "") for key in ("id", "name", "email", "role")})
        headers = include_user_info_headers({"Authorization": "Bearer " + service_key}, user)
        jwt_header = FORWARD_USER_INFO_HEADER_JWT
        if not headers.get(jwt_header):
            return json.dumps({"error": "Signed user forwarding is required; unsigned fallback rejected"})
        headers = {"Authorization": headers["Authorization"], jwt_header: headers[jwt_header]}
        if __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"description": "Reading public page with AgentCore Browser", "done": False}})
        try:
            timeout = aiohttp.ClientTimeout(total=60, connect=3)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(base + "/browser", json={"url": url}, headers=headers, allow_redirects=False) as response:
                    if response.status != 200:
                        raise ValueError("Browser request unavailable or rejected")
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        if len(raw) + len(chunk) > 131072:
                            raise ValueError("Browser response too large")
                        raw.extend(chunk)
                    document = json.loads(raw)
            text = document["text"]
            final_url = document["final_url"]
            title = document.get("title") or final_url
            if not isinstance(text, str) or len(text) > 20000 or not isinstance(final_url, str):
                raise ValueError("Invalid Browser document")
            if __event_emitter__:
                await __event_emitter__({"type": "citation", "data": {
                    "document": [text],
                    "metadata": [{"source": final_url, "requested_url": url}],
                    "source": {"name": title, "url": final_url},
                }})
            return json.dumps(document, ensure_ascii=False)
        except Exception:
            return json.dumps({"error": "Browser page loading failed or was rejected; no fallback attempted"})
        finally:
            if __event_emitter__:
                await __event_emitter__({"type": "status", "data": {"description": "Browser page loading finished", "done": True}})
