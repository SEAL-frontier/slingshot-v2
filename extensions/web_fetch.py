import re
import aiohttp
from cornnor.modules import tool


@tool(name="fetch_url", description="Fetch the text or JSON content from a public URL")
async def fetch_url(url: str, max_chars: int = 2000) -> str:
    """Fetches a URL and returns a cleaned snippet of its text content."""
    if not url.startswith(("http://", "https://")):
        return "Error: URL must begin with http:// or https://"

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CornnorBot/1.0; +https://github.com)"
    }
    timeout = aiohttp.ClientTimeout(total=10)

    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return f"HTTP error {response.status}: {response.reason}"

                content_type = response.headers.get("Content-Type", "")
                text = await response.text()

                # If HTML, strip script/style tags and basic tags
                if "html" in content_type:
                    clean = re.sub(r"<(script|style).*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
                    clean = re.sub(r"<[^>]+>", " ", clean)
                    clean = re.sub(r"\s+", " ", clean).strip()
                    text = clean

                if len(text) > max_chars:
                    return text[:max_chars] + f"\n... [Truncated, total length: {len(text)} characters]"
                return text
    except Exception as e:
        return f"Failed to fetch '{url}': {e}"
