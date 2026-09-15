"""Loopback-only adapter entrypoint; not an infrastructure deployment command."""

import os

import uvicorn

from web_capabilities.app import Settings, create_app
from web_capabilities.auth import Authenticator
from web_capabilities.browser import BrowserFetcher
from web_capabilities.gateway import GatewaySearchClient


def build_app():
    region = os.environ.get("AGENTCORE_WEB_REGION", "")
    if region not in {"us-east-1", "eu-west-1", "ap-northeast-1"}:
        raise ValueError("Select an explicitly supported AgentCore Web Search region")
    settings = Settings(
        search_enabled=os.environ.get("AGENTCORE_WEB_SEARCH_ENABLED") == "true",
        browser_enabled=os.environ.get("AGENTCORE_WEB_BROWSER_ENABLED") == "true",
        identity_header=os.environ.get("FORWARD_USER_INFO_HEADER_JWT", "X-OpenWebUI-User-Jwt"),
    )
    auth = Authenticator(
        os.environ.get("EXTERNAL_WEB_SEARCH_API_KEY", ""),
        os.environ.get("FORWARD_USER_INFO_HEADER_JWT_SECRET", ""),
    )
    search = GatewaySearchClient(
        region=region,
        gateway_url=os.environ.get("AGENTCORE_WEB_GATEWAY_URL", ""),
        tool_name=os.environ.get("AGENTCORE_WEB_SEARCH_TOOL_NAME", ""),
    ) if settings.search_enabled else None
    browser = BrowserFetcher(
        browser_identifier=os.environ.get("AGENTCORE_WEB_BROWSER_ID", ""),
        allowed_hosts=tuple(host.strip() for host in os.environ.get("AGENTCORE_WEB_BROWSER_HOSTS", "").split(",") if host.strip()),
        enabled=True,
        network_policy_ready=os.environ.get("AGENTCORE_WEB_BROWSER_NETWORK_POLICY_READY") == "true",
        region=region,
    ) if settings.browser_enabled else None
    return create_app(auth, search_client=search, browser_loader=browser, settings=settings)


if __name__ == "__main__":
    uvicorn.run(build_app(), host="127.0.0.1", port=8090, access_log=False, log_level="warning")
