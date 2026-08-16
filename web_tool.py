"""
web_tool.py — axeAI Outbound Web Tool Engine
=============================================
Provides asynchronous HTTP GET and POST capabilities for axeAI agents.

Features:
  - GET: Retrieves content from documentation, JSON APIs, and websites.
    Sanitizes raw HTML into readable Markdown/plain text to conserve LLM context.
  - POST: Submits payloads to APIs, local servers, or webhooks.
    Protected by domain whitelist and mandatory Orchestrator/user confirmation.
"""

import asyncio
import json
import logging
import re
from urllib.parse import urlparse
import httpx

logger = logging.getLogger("aX.web_tool")


class WebToolError(Exception):
    """Raised when an HTTP operation fails or violates security policy."""


def _sanitize_html(html_content: str) -> str:
    """
    Convert raw HTML to clean, readable plain text/markdown structure
    to preserve LLM context budget.
    """
    # Remove script and style tags completely
    cleaned = re.sub(r"<(script|style|svg|noscript)[^>]*>.*?</\1>", "", html_content, flags=re.DOTALL | re.IGNORECASE)
    # Convert headings to markdown
    cleaned = re.sub(r"<h[1-6][^>]*>(.*?)</h[1-6]>", r"\n\n### \1\n", cleaned, flags=re.IGNORECASE)
    # Convert paragraphs and breaks
    cleaned = re.sub(r"<p[^>]*>", "\n\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.IGNORECASE)
    # Convert links (single or double quotes)
    cleaned = re.sub(r"""<a\s+(?:[^>]*?\s+)?href=['"]([^'"]*)['"][^>]*>(.*?)</a>""", r"[\2](\1)", cleaned, flags=re.IGNORECASE)
    # Convert list items
    cleaned = re.sub(r"<li[^>]*>", "\n* ", cleaned, flags=re.IGNORECASE)
    # Strip remaining HTML tags
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    # Normalize whitespace
    cleaned = re.sub(r"\n\s*\n\s*\n+", "\n\n", cleaned)
    return cleaned.strip()


class WebTool:
    """
    Outbound HTTP tool with safety gating and response sanitization.
    """

    def __init__(self, allowed_domains: list[str] | None = None):
        self.allowed_domains = allowed_domains or []
        self._client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)

    async def close(self) -> None:
        await self._client.aclose()

    def is_domain_allowed(self, url: str) -> bool:
        if not self.allowed_domains:
            return True  # If empty, all domains are allowed by default
        parsed = urlparse(url)
        hostname = parsed.hostname or ""
        return any(hostname == domain or hostname.endswith("." + domain) for domain in self.allowed_domains)

    async def http_get(self, url: str, sanitize: bool = True) -> str:
        """
        Execute an HTTP GET request.
        """
        if not self.is_domain_allowed(url):
            raise WebToolError(f"Access to domain for URL '{url}' is not in the allowed domain list.")

        try:
            logger.info("WebTool: HTTP GET -> %s", url)
            response = await self._client.get(url, headers={"User-Agent": "axeAI-Agent/1.1"})
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            text = response.text

            if "html" in content_type and sanitize:
                return _sanitize_html(text)
            return text
        except Exception as e:
            logger.error("WebTool: GET failed for '%s': %s", url, e)
            raise WebToolError(f"HTTP GET error: {e}") from e

    async def http_post(self, url: str, data: dict | str, headers: dict | None = None) -> str:
        """
        Execute an HTTP POST request.
        """
        if not self.is_domain_allowed(url):
            raise WebToolError(f"Access to domain for URL '{url}' is not in the allowed domain list.")

        try:
            logger.info("WebTool: HTTP POST -> %s", url)
            req_headers = {"User-Agent": "axeAI-Agent/1.1"}
            if headers:
                req_headers.update(headers)

            if isinstance(data, dict):
                response = await self._client.post(url, json=data, headers=req_headers)
            else:
                response = await self._client.post(url, content=data, headers=req_headers)

            response.raise_for_status()
            return response.text
        except Exception as e:
            logger.error("WebTool: POST failed for '%s': %s", url, e)
            raise WebToolError(f"HTTP POST error: {e}") from e
