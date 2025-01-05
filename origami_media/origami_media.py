import asyncio
from enum import Enum, auto
from typing import Any, Dict, Optional, Type, cast

from maubot.handlers import command, event
from maubot.matrix import MaubotMessageEvent
from maubot.plugin_base import Plugin
from mautrix.types import EventType
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from .handlers.command_handler import CommandHandler
from .handlers.dependency_handler import DependencyHandler
from .handlers.display_handler import DisplayHandler
from .handlers.media_handler import MediaHandler
from .handlers.url_handler import UrlHandler


class Intent(Enum):
    DEFAULT = auto()
    QUERY = auto()
    AUDIO = auto()


class QueueItem:
    def __init__(
        self,
        intent: Intent,
        event: MaubotMessageEvent,
        data: Optional[Dict[str, Any]] = None,
    ):
        self.intent = intent
        self.event = event
        self.data = data or {}

    def update(self, intent: Intent, **data_updates):
        self.intent = intent
        self.data.update(data_updates)


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper):
        helper.copy("meta")
        helper.copy("whitelist")
        helper.copy("ytdlp")
        helper.copy("ffmpeg")
        helper.copy("file")
        helper.copy("queue")
        helper.copy("command")

    @property
    def meta(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("meta", {}))

    @property
    def whitelist(self) -> list[str]:
        return cast(list[str], self.get("whitelist", []))

    @property
    def ytdlp(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("ytdlp", {}))

    @property
    def ffmpeg(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("ffmpeg", {}))

    @property
    def file(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("file", {}))

    @property
    def queue(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("queue", {}))

    @property
    def command(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.get("command", {}))


class OrigamiMedia(Plugin):
    config: Config

    async def start(self):
        self.log.info(f"Starting Origami Media")
        await super().start()

        if not self.config:
            raise Exception("Config is not initialized")

        self.config.load_and_update()

        self.dependency_handler = DependencyHandler(log=self.log)
        self.url_handler = UrlHandler(log=self.log, config=self.config)
        self.media_handler = MediaHandler(
            log=self.log, config=self.config, client=self.client, http=self.http
        )
        self.display_handler = DisplayHandler(
            log=self.log, config=self.config, client=self.client
        )
        self.command_handler = CommandHandler(
            config=self.config, log=self.log, http=self.http
        )
        self.event_queue = asyncio.Queue(maxsize=self.config.queue.get("max_size", 100))
        self.url_event_queue = asyncio.Queue()
        self.media_event_queue = asyncio.Queue()

        self.workers = [
            asyncio.create_task(self._url_worker(), name="url_worker"),
            asyncio.create_task(self._media_worker(), name="media_worker"),
            asyncio.create_task(self._display_worker(), name="display_worker"),
        ]

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @cast(Any, event.on)(EventType.ROOM_MESSAGE)
    async def main(self, event: MaubotMessageEvent) -> None:
        if not event.content.msgtype.is_text or event.sender == self.client.mxid:
            return

        if cast(str, event.content.body).startswith("!"):
            await self.command_controller(event=event)
            return

        if not self.config.meta.get("enable_passive_url_detection", False):
            return

        if "http" not in event.content.body and "www" not in event.content.body:
            return

        try:
            item = QueueItem(intent=Intent.DEFAULT, event=event, data={})
            hourglass_reaction_event_id = await event.react("⏳")
            item.data["active_reaction_id"] = hourglass_reaction_event_id
            self.event_queue.put_nowait(item)

        except asyncio.QueueFull:
            self.log.warning("Message queue is full. Dropping incoming message.")
        except Exception as e:
            self.log.error(f"{e}")

    async def _url_worker(self) -> None:
        while True:
            try:
                item: QueueItem = await self.event_queue.get()
                await self.client.redact(
                    room_id=item.event.room_id, event_id=item.data["active_reaction_id"]
                )
                loading_reaction_event_id = await item.event.react("🔄")
                item.data["active_reaction_id"] = loading_reaction_event_id

                if item.intent == Intent.DEFAULT:
                    item.data["valid_urls"] = await self.url_handler.process(item.event)
                    await self.url_event_queue.put(item)

                elif item.intent == Intent.QUERY:
                    url = await self.command_handler.query_image_controller(
                        query=item.data["query"],
                        provider=item.data["provider"],
                    )
                    item.data["valid_urls"] = self.url_handler.process_string(
                        message=url
                    )
                    await self.url_event_queue.put(item)

            except asyncio.CancelledError:
                self.log.info("[url Worker] Shutting down gracefully.")
                break
            except Exception as e:
                self.log.error(
                    f"[url Worker] Failed to process item for event: {getattr(item.event, 'event_id', 'N/A')} - {e}"
                )
                await self.client.redact(
                    room_id=item.event.room_id, event_id=item.data["active_reaction_id"]
                )
            finally:
                self.event_queue.task_done()

    async def _media_worker(self) -> None:
        while True:
            try:
                item: QueueItem = await self.url_event_queue.get()

                if item.intent == Intent.DEFAULT or item.intent == Intent.QUERY:
                    item.data["processed_media"] = await self.media_handler.process(
                        urls=item.data["valid_urls"], event=item.event
                    )
                    await self.media_event_queue.put(item)

            except asyncio.CancelledError:
                self.log.info("[Media Worker] Shutting down gracefully.")
                break
            except Exception as e:
                self.log.error(
                    f"[Media Worker] Failed to process media for  {getattr(item.event, 'event_id', 'N/A')} - {e}"
                )
                await self.client.redact(
                    room_id=item.event.room_id, event_id=item.data["active_reaction_id"]
                )
            finally:
                self.url_event_queue.task_done()

    async def _display_worker(self) -> None:
        while True:
            try:
                item: QueueItem = await self.media_event_queue.get()

                if item.intent == Intent.DEFAULT:
                    await self.display_handler.render(
                        media=item.data["processed_media"], event=item.event
                    )

                elif item.intent == Intent.QUERY:
                    await self.display_handler.render(
                        media=item.data["processed_media"],
                        event=item.event,
                        reply=False,
                    )

            except asyncio.CancelledError:
                self.log.info("[Display Worker] Shutting down gracefully.")
                break
            except Exception as e:
                self.log.error(
                    f"[Display Worker] Failed to render for {getattr(item.event, 'event_id', 'N/A')} in display_handler: {e}"
                )
            finally:
                self.media_event_queue.task_done()
                await self.client.redact(
                    room_id=item.event.room_id, event_id=item.data["active_reaction_id"]
                )

    async def stop(self) -> None:
        self.log.info("Shutting down workers...")
        for task in self.workers:
            task.cancel()

        results = await asyncio.gather(*self.workers, return_exceptions=True)
        for task, result in zip(self.workers, results):
            if isinstance(result, Exception) and not isinstance(
                result, asyncio.CancelledError
            ):
                self.log.error(
                    f"Task {task.get_name()} failed during shutdown: {result}"
                )

        self.log.info("All workers stopped cleanly.")
        await super().stop()

    async def command_controller(self, event: MaubotMessageEvent):
        if not self.config.meta.get("enable_commands", False):
            return

        body = cast(str, event.content.body)
        parts = body.split(" ", 1)
        command = parts[0]
        argument = parts[1] if len(parts) > 1 else ""

        query_commands = {
            "!tenor": "tenor",
            "!gif": "tenor",
            "!tr": "tenor",
            "!unsplash": "unsplash",
            "!img": "unsplash",
            "!uh": "unsplash",
            "lexica": "lexica",
            "lex": "lexica",
            "la": "lexica",
        }
        try:
            if command == "!dl":
                item = QueueItem(intent=Intent.DEFAULT, event=event, data={})
                hourglass_reaction_event_id = await event.react("⏳")
                item.data["active_reaction_id"] = hourglass_reaction_event_id
                self.event_queue.put_nowait(item)

            elif command in query_commands:
                provider = query_commands[command]
                item = QueueItem(intent=Intent.QUERY, event=event, data={})
                hourglass_reaction_event_id = await event.react("⏳")
                item.data["active_reaction_id"] = hourglass_reaction_event_id
                item.data["query"] = argument
                item.data["provider"] = provider
                self.event_queue.put_nowait(item)

            elif command == "audio":
                return
        except asyncio.QueueFull:
            self.log.warning("Message queue is full. Dropping incoming message.")
