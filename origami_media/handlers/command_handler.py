import random
import urllib.parse
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from aiohttp import ClientSession
    from maubot.matrix import MaubotMessageEvent
    from mautrix.util.logging.trace import TraceLogger

    from origami_media.origami_media import Config

    from .display_handler import DisplayHandler
    from .media_handler import MediaHandler


class CommandHandler:

    def __init__(self, config: "Config", log: "TraceLogger", http: "ClientSession"):
        self.config = config
        self.log = log
        self.http = http

    async def _query_image(
        self, query: str, provider: str, api_key: str
    ) -> Optional[str]:

        if provider == "tenor":
            rating = "off"
            api_version = "v2"
            url_params = urllib.parse.urlencode(
                {"q": query, "key": api_key, "contentfilter": rating}
            )
            base_url = f"https://g.tenor.com/{api_version}/search?{url_params}"
            async with self.http.get(base_url) as response:
                data = await response.json()
                results = data.get("results", [])
                if not results:
                    return None
                result = random.choice(results)
                gif = (
                    result["media_formats"]["gif"]
                    if api_version == "v2"
                    else result["media"][0]["gif"]
                )
                link = gif["url"]
            return link

        if provider == "unsplash":
            url_params = urllib.parse.urlencode({"query": query, "client_id": api_key})
            base_url = f"https://api.unsplash.com/search/photos?{url_params}"
            async with self.http.get(base_url) as response:
                data = await response.json()
                results = data.get("results", [])
                if not results:
                    return None
                result = random.choice(results)
                link = result["urls"]["regular"]
            return link

        else:
            self.log.error(f"Unsupported provider: {provider}")
            return None

    async def query_image_controller(
        self,
        event: "MaubotMessageEvent",
        query: str,
        provider: str,
        media_handler: "MediaHandler",
        display_handler: "DisplayHandler",
    ) -> None:
        if not query:
            return

        api_key = self.config.command["query_image"][f"{provider}_api_key"]

        url = await self._query_image(query=query, provider=provider, api_key=api_key)
        if not url:
            return

        processed_media, _ = await media_handler.process(urls=[url], event=event)
        if not processed_media:
            return

        await display_handler.render(media=processed_media, event=event, reply=False)
