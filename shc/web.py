import abc
import asyncio
import json
import logging
from typing import List, Dict, Any, Type

import aiohttp.web

from .base import Subscribable, Writable, Reading, T
from .supervisor import register_interface

logger = logging.getLogger(__name__)


class WebServer:
    def __init__(self, host: str, port: int, index_name: str):
        self.host = host
        self.port = port
        self.index_name = index_name
        self._pages: Dict[str, WebPage] = {}
        self.widgets: Dict[int, WebWidget] = {}
        self._app = aiohttp.web.Application()
        self._app.add_routes([
            aiohttp.web.get("/", self._index_handler),
            aiohttp.web.get("/{name}/", self._page_handler),
            aiohttp.web.get("/ws", self._websocket_handler),
        ])
        register_interface(self)

    async def run(self) -> None:
        logger.info("Starting up web server on %s:%s ...", self.host, self.port)
        self._runner = aiohttp.web.AppRunner(self._app)
        await self._runner.setup()
        site = aiohttp.web.TCPSite(self._runner, self.host, self.port)
        await site.start()

    async def stop(self) -> None:
        logger.info("Cleaning up AppRunner ...")
        await self._runner.cleanup()

    def page(self, name: str) -> "WebPage":
        if name in self._pages:
            return self._pages[name]
        else:
            page = WebPage(self, name)
            self._pages[name] = page
            return page

    async def _index_handler(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        raise aiohttp.web.HTTPFound("/{}/".format(self.index_name))

    async def _page_handler(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        try:
            page = self._pages[request.match_info['name']]
        except KeyError:
            raise aiohttp.web.HTTPNotFound()
        return await page.generate(request)

    async def _websocket_handler(self, request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)

        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._websocket_dispatch(ws, msg)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.info('ws connection closed with exception %s', ws.exception())
        logger.debug('websocket connection closed')
        for widget in self.widgets.values():
            widget.ws_unsubscribe(ws)
        return ws

    async def _websocket_dispatch(self, ws, msg) -> None:
        data = json.loads(msg)
        # TODO error handling
        action = data["action"]
        if action == 'subscribe':
            await self.widgets[data["widget"]].ws_subscribe(ws)
        elif action == 'write':
            await self.widgets[data["widget"]].write(data["value"], [ws])


class WebPage:
    def __init__(self, server: WebServer, name: str):
        self.server = server
        self.name = name
        self.items: List[WebItem] = []

    def add_item(self, item: "WebItem"):
        self.items.append(item)
        self.server.widgets.update({id(widget): widget for widget in item.widgets})

    async def generate(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        # TODO use Jinja2 template
        body = "<!DOCTYPE html><html><head></head><body>\n"\
               + "\n".join(item.render() for item in self.items)\
               + "\n</body></html>"
        return aiohttp.web.Response(body=body, content_type="text/html", charset='utf-8')


class WebItem(metaclass=abc.ABCMeta):
    widgets: List["WebWidget"] = []

    @abc.abstractmethod
    def render(self) -> str:
        pass


class WebWidget(Reading[T], Writable[T], Subscribable[T], metaclass=abc.ABCMeta):
    def __init__(self, type_: Type[T]):
        self.type = type_
        super().__init__()
        self.subscribed_websockets = set()

    async def write(self, value: T, source: List[Any]):
        data = json.dumps({'widget': id(self),
                           'value': value})
        await asyncio.gather(*(ws.send_str(data) for ws in self.subscribed_websockets))
        await self._publish(value, source)

    async def ws_subscribe(self, ws):
        self.subscribed_websockets.add(ws)
        data = json.dumps({'widget': id(self),
                           'value': await self._from_provider()})
        await ws.send_str(data)

    def ws_unsubscribe(self, ws):
        self.subscribed_websockets.discard(ws)

    # TODO add connect() method


class Switch(WebWidget, WebItem):
    def __init__(self, label: str):
        super().__init__(bool)
        self.label = label
        self.widgets = [self]

    def render(self) -> str:
        # TODO use Jinja2 templates
        return "<div><input type=\"checkbox\" data-widget=\"switch\" data-id=\"{id}\" /> {label}</div>"\
            .format(label=self.label, id=id(self))
