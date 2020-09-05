# Copyright 2020 Michael Thies <mail@mhthies.de>
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in compliance with
# the License. You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.

import abc
import asyncio
import itertools
import json
import logging
import os
import pathlib
import weakref
from json import JSONDecodeError
from typing import Dict, Iterable, Union, List, Set, Any, Optional, Tuple, Generic, Type

import aiohttp.web
import jinja2
import markupsafe
from aiohttp import WSCloseCode

from ..base import Reading, T, Writable, Subscribable
from ..conversion import SHCJsonEncoder, from_json
from ..supervisor import register_interface

logger = logging.getLogger(__name__)

jinja_env = jinja2.Environment(
    loader=jinja2.PackageLoader('shc.web', 'templates'),
    autoescape=jinja2.select_autoescape(['html', 'xml']),
    enable_async=True,
    trim_blocks=True,
    lstrip_blocks=True,
)
jinja_env.filters['id'] = id


class WebServer:
    """
    A SHC interface to provide the web user interface and a REST+websocket API for interacting with Connectable objects.
    """
    def __init__(self, host: str, port: int, index_name: Optional[str] = None, root_url: str = "/"):
        """
        :param host: The listening host. Use "" to listen on all interfaces or "localhost" to listen only on the
            loopback interface.
        :param port: The port to listen on
        :param index_name: Name of the `WebPage`, the root URL redirects to. If None, the root URL returns an HTTP 404.
        :param root_url: The base URL, at witch the user will reach this server. Used to construct internal links. May
            be an absolute URI (like "https://myhost:8080/shc/") or an absolute-path reference (like "/shc/"). Defaults
            to "/". Note: This does not affect the routes of this HTTP server. It is only relevant, if you use an HTTP
            reverse proxy in front of this application, which serves the application in a sub path.
        """
        self.host = host
        self.port = port
        self.index_name = index_name
        self.root_url = root_url

        # a dict of all `WebPage`s by their `name` for rendering them in the `_page_handler`
        self._pages: Dict[str, WebPage] = {}
        # a dict of all `WebConnector`s by their Python object id for routing incoming websocket mesages
        self.connectors: Dict[int, WebUIConnector] = {}
        # a dict of all `WebApiObject`s by their name for handling incoming HTTP requests and subscribe messages
        self._api_objects: Dict[str, WebApiObject] = {}
        # a set of all open websockets to close on graceful shutdown
        self._websockets: weakref.WeakSet[aiohttp.web.WebSocketResponse] = weakref.WeakSet()
        # a set of all open tasks to close on graceful shutdown
        self._associated_tasks: weakref.WeakSet[asyncio.Task] = weakref.WeakSet()
        # data structure of the user interface's main menu
        # The structure looks as follows:
        # [('Label', 'page_name'),
        #  ('Submenu label', [
        #     ('Label 2', 'page_name2'), ...
        #   ]),
        #  ...]
        # TODO provide interface for easier setting of this structure
        self.ui_menu_entries: List[Tuple[Union[str, markupsafe.Markup], Union[str, Tuple]]] = []
        # List of all static js URLs to be included in the user interface pages
        self._js_files = [
            "static/jquery-3.min.js",
            "static/semantic-ui/components/checkbox.min.js",
            "static/semantic-ui/components/dropdown.min.js",
            "static/semantic-ui/components/slider.min.js",
            "static/semantic-ui/components/sidebar.min.js",
            "static/semantic-ui/components/transition.min.js",
            "static/iro.min.js",
            "static/main.js",
        ]
        # List of all static css URLs to be included in the user interface pages
        self._css_files = [
            "static/semantic-ui/semantic.min.css",
            "static/main.css",
        ]
        # A dict of all static files served by the application. Used to make sure, any of those is only served at one
        # path, when added via `serve_static_file()` multiple times.
        self.static_files: Dict[pathlib.Path, str] = {}

        # The actual aiohttp web app
        self._app = aiohttp.web.Application()
        self._app.add_routes([
            aiohttp.web.get("/", self._index_handler),
            aiohttp.web.get("/page/{name}/", self._page_handler, name='show_page'),
            aiohttp.web.get("/ws", self._ui_websocket_handler),
            aiohttp.web.static('/static', os.path.join(os.path.dirname(__file__), 'static')),
            aiohttp.web.get("/api/v1/ws", self._api_websocket_handler),
            aiohttp.web.get("/api/v1/object/{name}", self._api_get_handler),
            aiohttp.web.post("/api/v1/object/{name}", self._api_post_handler),
        ])

        register_interface(self)

    async def start(self) -> None:
        logger.info("Starting up web server on %s:%s ...", self.host, self.port)
        for connector in itertools.chain.from_iterable(page.get_connectors() for page in self._pages.values()):
            self.connectors[id(connector)] = connector
        self._runner = aiohttp.web.AppRunner(self._app)
        await self._runner.setup()
        site = aiohttp.web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        # aiohttp's Runner or Site do not provide a good method to await the stopping of the server. Thus we use our own
        # Event for that purpose.
        self._stopped = asyncio.Event()

    async def wait(self) -> None:
        await self._stopped.wait()

    async def stop(self) -> None:
        logger.info("Closing open websockets ...")
        for ws in set(self._websockets):
            await ws.close(code=WSCloseCode.GOING_AWAY, message='Server shutdown')
        for task in set(self._associated_tasks):
            task.cancel()
        logger.info("Cleaning up AppRunner ...")
        await self._runner.cleanup()
        self._stopped.set()

    def page(self, name: str) -> "WebPage":
        """
        Create a new WebPage with a given name.

        If there is already a page with that name existing, it will be returned.

        :param name: The `name` of the page, which is used in the page's URL to identify it
        :return: The new WebPage object or the existing WebPage object with that name
        """
        if name in self._pages:
            return self._pages[name]
        else:
            page = WebPage(self, name)
            self._pages[name] = page
            return page

    def api(self, type_: Type, name: str) -> "WebApiObject":
        """
        Create a new API endpoint with a given name and type.

        :param type_: The value type of the API endpoint object. Used as the *Connectable* object's `type` attribute and
            for JSON-decoding/encoding the values transmitted via the API.
        :param name: The name of the API object, which is the distinguishing part of the REST-API endpoint URL and used
            to identify the object in the websocket API.
        :return: A *Connectable* object that represents the API endpoint.
        """
        if name in self._api_objects:
            existing = self._api_objects[name]
            if existing.type is not type_:
                raise TypeError("Type {} does not match type {} of existing API object with same name"
                                .format(type_, existing.type))
            return existing
        else:
            api_object = WebApiObject(type_, name)
            self._api_objects[name] = api_object
            return api_object

    async def _index_handler(self, _request: aiohttp.web.Request) -> aiohttp.web.Response:
        if not self.index_name:
            return aiohttp.web.HTTPNotFound()
        return aiohttp.web.HTTPFound(self._app.router['show_page'].url_for(name=self.index_name))

    async def _page_handler(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        try:
            page = self._pages[request.match_info['name']]
        except KeyError:
            raise aiohttp.web.HTTPNotFound()

        template = jinja_env.get_template('page.htm')
        body = await template.render_async(title=page.name, segments=page.segments, menu=self.ui_menu_entries,
                                           root_url=self.root_url, js_files=self._js_files, css_files=self._css_files)
        return aiohttp.web.Response(body=body, content_type="text/html", charset='utf-8')

    async def _ui_websocket_handler(self, request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        self._websockets.add(ws)

        msg: aiohttp.WSMessage
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    asyncio.create_task(self._ui_websocket_dispatch(ws, msg))
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.info('UI websocket connection closed with exception %s', ws.exception())
        finally:
            logger.debug('UI websocket connection closed')
            # Make sure the websocket is removed as a subscriber from all WebDisplayDatapoints
            self._websockets.discard(ws)
            for connector in self.connectors.values():
                await connector.websocket_close(ws)
            return ws

    async def _ui_websocket_dispatch(self, ws: aiohttp.web.WebSocketResponse, msg: aiohttp.WSMessage) -> None:
        message = msg.json()
        try:
            connector = self.connectors[message["id"]]
        except KeyError:
            logger.error("Could not route message from websocket to connector, since no connector with id %s is "
                         "known.", message['id'])
            return
        if 'v' in message:
            await connector.from_websocket(message['v'], ws)
        elif 'sub' in message:
            await connector.websocket_subscribe(ws)
        else:
            logger.warning("Don't know how to handle websocket message: %s", message)

    async def _api_websocket_handler(self, request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        self._websockets.add(ws)

        msg: aiohttp.WSMessage
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    asyncio.create_task(self._api_websocket_dispatch(request, ws, msg))
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.info('API websocket connection closed with exception %s', ws.exception())
        finally:
            logger.debug('API websocket connection closed')
            self._websockets.discard(ws)
            for api_object in self._api_objects.values():
                api_object.websocket_close(ws)
            return ws

    async def _api_websocket_dispatch(self, request: aiohttp.web.Request, ws: aiohttp.web.WebSocketResponse,
                                      msg: aiohttp.WSMessage) -> None:
        try:
            message = msg.json()
        except JSONDecodeError:
            logger.warning("Websocket API message from %s is not a valid JSON string: %s", request.remote, msg.data)
            await ws.send_json({'status': 400, 'error': "Could not parse message as JSON: {}".format(msg.data)})
            return

        try:
            name = message["name"]
            action = message["action"]
            handle = message.get("handle")
        except KeyError:
            logger.warning("Websocket API message from %s without 'name' or 'action' field: %s", request.remote,
                           message)
            await ws.send_json({'status': 422,
                                'error': "Message does not include a 'name' and an 'action' field"})
            return
        result = {'status': 204,
                  'name': name,
                  'action': action,
                  'handle': handle}
        try:
            obj = self._api_objects[name]
        except KeyError:
            logger.warning("Could not find API object %s, requested by %s", name, request.remote)
            result['status'] = 404
            result['error'] = "There is no API object with name '{}'".format(name)
            await ws.send_json(result)
            return

        try:
            # subscribe action
            if action == "subscribe":
                logger.debug("got websocket subscribe request for API object %s from %s", name, request.remote)
                await obj.websocket_subscribe(ws)

            # post action
            elif action == "post":
                value_exists = False
                try:
                    value = message["value"]
                    value_exists = True
                except KeyError:
                    result['status'] = 422
                    result['error'] = "message does not include a 'value' field"
                    logger.warning("Websocket API POST message from %s without 'value' field: %s", request.remote,
                                   message)
                if value_exists:
                    logger.debug("got post request for API object %s via websocket from %s with value %s",
                                 name, request.remote, value)
                    try:
                        await obj.http_post(value, ws)
                    except (ValueError, TypeError) as e:
                        logger.warning("Error while updating API object %s with value via websocket from %s (error was "
                                       "%s): %s", name, request.remote, e, value)
                        result['status'] = 422
                        result['error'] = "Could not use provided value to update API object: {}".format(e)

            # get action
            elif action == "get":
                logger.debug("got get request for API object %s via websocket from %s", name, request.remote)
                value = await obj.http_get()
                result['status'] = 200 if value is not None else 409
                result['value'] = value

            else:
                logger.warning("Unknown websocket API action '%s', requested by %s", action, request.remote)
                result['status'] = 422
                result['error'] = "Not a valid action: '{}'".format(action)
        except Exception as e:
            logger.error("Error while processing API websocket message from %s: %s", request.remote, message,
                         exc_info=e)
            result['status'] = 500
            result['error'] = "Internal server error while processing message"

        # Finally, send a response
        await ws.send_str(json.dumps(result, cls=SHCJsonEncoder))

    async def _api_get_handler(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        try:
            api_object = self._api_objects[request.match_info['name']]
        except KeyError:
            name_failsafe = request.match_info.get('name', '<undefined>')
            logger.warning("Could not find API object %s, requested by %s", name_failsafe, request.remote)
            raise aiohttp.web.HTTPNotFound(reason="Could not find API Object with name {}"
                                           .format(name_failsafe))
        # Parse `wait` and `timeout` from request query string
        wait = 'wait' in request.query
        timeout = 30
        if wait and request.query['wait']:
            try:
                timeout = float(request.query['wait'])
            except ValueError as e:
                raise aiohttp.web.HTTPBadRequest(reason="Could not parse 'wait' query parameter's value as float: {}"
                                                 .format(e))

        # if `wait`: Make this Request gracefully stoppable on shutdown by registering it for
        if wait:
            self._associated_tasks.add(asyncio.current_task())

        # Now, let's actually call http_get of the API object
        # If `wait`, this will await a new value or the `timeout`.
        changed, value, etag = await api_object.http_get(wait, timeout, request.headers.get('If-None-Match'))

        # If not changed (either when `wait` and timeout is reached) or if not `wait` and `If-None-Match` indicates
        # unchanged value, return HTTP 304 Not Modified
        if not changed:
            return aiohttp.web.HTTPNotModified(headers={'ETag': etag})
        else:
            return aiohttp.web.Response(status=200 if value is not None else 409,
                                        headers={'ETag': etag},
                                        body=json.dumps(value, cls=SHCJsonEncoder),
                                        content_type="application/json",
                                        charset='utf-8')

    async def _api_post_handler(self, request: aiohttp.web.Request) -> aiohttp.web.Response:
        text = await request.text()
        try:
            data = json.loads(text)
        except JSONDecodeError as e:
            logger.warning("Invalid JSON body POSTed from %s to %s (error was: %s): %s",
                           request.remote, request.url, e, text)
            raise aiohttp.web.HTTPBadRequest(reason="Could not parse request body as json: {}".format(str(e)))

        try:
            name = request.match_info['name']
            api_object = self._api_objects[name]
        except KeyError:
            name_failsafe = request.match_info.get('name', '<undefined>')
            logger.warning("Could not find API object %s, requested by %s", name_failsafe, request.remote)
            raise aiohttp.web.HTTPNotFound(reason="Could not find API Object with name {}"
                                           .format(name_failsafe))
        try:
            await api_object.http_post(data, request)
        except (ValueError, TypeError) as e:
            logger.warning("Error while updating API object %s with value from %s (error was %s): %s", name,
                           request.remote, e, data)
            raise aiohttp.web.HTTPUnprocessableEntity(reason="Could not use provided value to update API object: {}"
                                                      .format(e))
        return aiohttp.web.HTTPNoContent()

    def serve_static_file(self, path: pathlib.Path) -> str:
        """
        Register a static file to be served on this HTTP server.

        The URL is automatically chosen, based on the file's name and existing static files.
        If the same path has already been added as a static file, its existing static URL is returned instead of
        creating a new one.

        This method should primarily be used by WebPageItem implementations within their
        :meth:`WebPageItem.register_with_server` method.

        :param path: The path of the local file to be served as a static file
        :return: The URL of the static file, as a path, relative to the server's root URL, without leading slash. For
            using it within the web UI's HTML code, the server's `root_url` must be prepended.
        """
        path = path.absolute()
        if path in self.static_files:
            return self.static_files[path]
        final_file_name = path.name
        i = 0
        while final_file_name in self.static_files:
            final_file_name = "{}_{:04d}.{}".format(path.stem, i, path.suffix)
        final_path = 'addon/{}'.format(final_file_name)
        self.static_files[path] = final_path

        # Unfortunately, aiohttp.web.static can only serve directories. We want to serve a single file here.
        async def send_file(_request):
            return aiohttp.web.FileResponse(path)
        self._app.add_routes([aiohttp.web.get("/" + final_path, send_file)])

        return final_path

    def add_js_file(self, path: pathlib.Path) -> None:
        """
        Register an additional static JavaScript file to be included in the web UI served by this server.

        This method adds the given path as a static file to the webserver and includes its URL into every web UI page
        using a `<script>` tag in the HTML head.
        If the same file has already been added as a static file to the webserver, this method does nothing.

        :param path: Local filesystem path of the JavaScript file to be included
        """
        if path in self.static_files:
            return
        self._js_files.append(self.serve_static_file(path))

    def add_css_file(self, path: pathlib.Path) -> None:
        """
        Register an additional static CSS file to be included in the web UI served by this server.

        This method adds the given path as a static file to the webserver and includes its URL into every web UI page
        using a `<link rel="stylesheet">` tag in the HTML head.
        If the same file has already been added as a static file to the webserver, this method does nothing.

        :param path: Local filesystem path of the CSS file to be included
        """
        if path in self.static_files:
            return
        self._css_files.append(self.serve_static_file(path))


class WebConnectorContainer(metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def get_connectors(self) -> Iterable["WebUIConnector"]:
        pass


class WebPage(WebConnectorContainer):
    def __init__(self, server: WebServer, name: str):
        self.server = server
        self.name = name
        self.segments: List["_WebPageSegment"] = []

    def add_item(self, item: "WebPageItem"):
        if not self.segments:
            self.new_segment()
        self.segments[-1].items.append(item)
        item.register_with_server(self, self.server)

    def get_connectors(self) -> Iterable["WebUIConnector"]:
        return itertools.chain.from_iterable(item.get_connectors() for item in self.segments)

    def new_segment(self, title: Optional[str] = None, same_column: bool = False, full_width: bool = False):
        self.segments.append(_WebPageSegment(title, same_column, full_width))


class _WebPageSegment(WebConnectorContainer):
    def __init__(self, title: Optional[str], same_column: bool, full_width: bool):
        self.title = title
        self.same_column = same_column
        self.full_width = full_width
        self.items: List[WebPageItem] = []

    def get_connectors(self) -> Iterable[Union["WebUIConnector"]]:
        return itertools.chain.from_iterable(item.get_connectors() for item in self.items)


class WebPageItem(WebConnectorContainer, metaclass=abc.ABCMeta):
    def register_with_server(self, page: WebPage, server: WebServer) -> None:
        """
        Called when the WebPageItem is added to a WebPage.

        It may be used to get certain information about the WebPage or the WebServer or register required static files
        with the WebServer, using :meth:`WebServer.serve_static_file`, :meth:`WebServer.add_js_file`,
        :meth:`WebServer.serve_static_file`.

        :param page: The WebPage, this WebPageItem is added to.
        :param server: The WebServer, the WebPage (and thus, from now on, this WebPageItem) belongs to.
        """
        pass

    @abc.abstractmethod
    async def render(self) -> str:
        pass


class WebUIConnector(WebConnectorContainer, metaclass=abc.ABCMeta):
    """
    An abstract base class for all objects that want to exchange messages with JavaScript UI Widgets via the websocket
    connection.

    For every Message received from a client websocket, the :meth:`from_websocket` method of the appropriate
    `WebUIConnector` is called. For this purpose, the :class:`WebServer` creates a dict of all WebConnectors in any
    registered :class:`WebPage` by their Python object id at startup. The message from the websocket is expected to have
    an `id` field which is used for the lookup.
    """
    def __init__(self):
        self.subscribed_websockets: Set[aiohttp.web.WebSocketResponse] = set()

    async def from_websocket(self, value: Any, ws: aiohttp.web.WebSocketResponse) -> None:
        """
        This method is called for incoming "value" messages from a client to this specific `WebUIConnector` object.

        :param value: The JSON-decoded 'value' field from the message from the websocket
        :param ws: The concrete websocket, the message has been received from.
        """
        pass

    async def websocket_subscribe(self, ws: aiohttp.web.WebSocketResponse) -> None:
        await self._websocket_before_subscribe(ws)
        self.subscribed_websockets.add(ws)

    async def _websocket_before_subscribe(self, ws: aiohttp.web.WebSocketResponse) -> None:
        """
        This method is called by :meth:`websocket_subscribe`, when a new websocket subscribes to this specific
        `WebUIConnector`, *before* the client is added to the `subscribed_websockets` variable.

        This can be used to send an initial value or other initialization methods to the client.
        """
        pass

    async def _websocket_publish(self, value: Any) -> None:
        logger.debug("Publishing value %s for %s for %s subscribed websockets ...",
                     value, id(self), len(self.subscribed_websockets))
        data = json.dumps({'id': id(self), 'v': value}, cls=SHCJsonEncoder)
        await asyncio.gather(*(ws.send_str(data) for ws in self.subscribed_websockets))

    async def websocket_close(self, ws: aiohttp.web.WebSocketResponse) -> None:
        self.subscribed_websockets.discard(ws)

    def get_connectors(self) -> Iterable["WebUIConnector"]:
        return (self,)

    def __repr__(self):
        return "{}<id={}>".format(self.__class__.__name__, id(self))


class WebDisplayDatapoint(Reading[T], Writable[T], WebUIConnector, metaclass=abc.ABCMeta):
    is_reading_optional = False

    def __init__(self):
        super().__init__()
        self.subscribed_websockets: Set[aiohttp.web.WebSocketResponse] = set()

    async def _write(self, value: T, origin: List[Any]):
        await self._websocket_publish(self.convert_to_ws_value(value))

    def convert_to_ws_value(self, value: T) -> Any:
        return value

    async def _websocket_before_subscribe(self, ws: aiohttp.web.WebSocketResponse) -> None:
        if self._default_provider is None:
            logger.error("Cannot handle websocket subscription for %s, since not read provider is registered.",
                         self)
            return
        logger.debug("New websocket subscription for widget id %s.", id(self))
        self.subscribed_websockets.add(ws)
        current_value = await self._from_provider()
        if current_value is not None:
            data = json.dumps({'id': id(self),
                               'v': self.convert_to_ws_value(current_value)},
                              cls=SHCJsonEncoder)
            await ws.send_str(data)


class WebActionDatapoint(Subscribable[T], WebUIConnector, metaclass=abc.ABCMeta):
    def convert_from_ws_value(self, value: Any) -> T:
        return from_json(self.type, value)

    async def from_websocket(self, value: Any, ws: aiohttp.web.WebSocketResponse) -> None:
        await self._publish(self.convert_from_ws_value(value), [ws])
        if isinstance(self, WebDisplayDatapoint):
            await self._websocket_publish(value)


class WebApiObject(Reading[T], Writable[T], Subscribable[T], Generic[T]):
    """
    *Connectable* object that represents an endpoint of the REST/websocket API.

    :ivar name: The name of this object in the REST/websocket API
    """
    is_reading_optional = False

    def __init__(self, type_: Type[T], name: str):
        self.type = type_
        super().__init__()
        self.name = name
        self.subscribed_websockets: Set[aiohttp.web.WebSocketResponse] = set()
        self.future: asyncio.Future[T] = asyncio.get_event_loop().create_future()

    async def _write(self, value: T, origin: List[Any]) -> None:
        await self._publish_http(value)

    async def http_post(self, value: Any, origin: Any) -> None:
        await self._publish(from_json(self.type, value), [origin])
        await self._publish_http(value)

    async def _publish_http(self, value: T) -> None:
        """
        Publish a new value to all subscribed websockets and waiting long-running poll requests.
        """
        self.future.set_result(value)
        self.future = asyncio.get_event_loop().create_future()
        data = json.dumps({'status': 200, 'name': self.name, 'value': value}, cls=SHCJsonEncoder)
        await asyncio.gather(*(ws.send_str(data) for ws in self.subscribed_websockets))

    async def websocket_subscribe(self, ws: aiohttp.web.WebSocketResponse) -> None:
        self.subscribed_websockets.add(ws)
        current_value = await self._from_provider()
        if current_value is not None:
            data = json.dumps({'status': 200, 'name': self.name, 'value': current_value}, cls=SHCJsonEncoder)
            await ws.send_str(data)

    def websocket_close(self, ws: aiohttp.web.WebSocketResponse) -> None:
        self.subscribed_websockets.discard(ws)

    async def http_get(self, wait: bool = False, timeout: float = 30, etag_match: Optional[str] = None
                       ) -> Tuple[bool, Any, str]:
        """
        Get the current value or await a new value.

        This method is used for normal GET requests and long-running polls that only return when a new value is
        awailable.

        :param wait: If True, the method awaits the receiving of a new value or the expiration of the timeout. If False,
            it simply *reads* and returns the current value
        :param timeout: If `wait` is True and no value arrives within `timeout` seconds, this method returns, with the
            first element in the result tuple set to True to indicate the timeout.
        :param etag_match: The `If-None-Match` header value provided by the client. Should be the `etag` from the
            client's last call to this method.
            With `wait=True`: If given and not equal to the id of the current future, this method assumes that the
            client missed a value and falls back to *read* and return the current value immediately. This way, we make
            sure that the client does not miss an update while renewing its poll request.
            With `wait=False`: Normal HTTP behaviour: If the etag does match the current future's is, we return
            with `changed=False`, which should result in an
        :return: A tuple (changed, value, etag).
            `changed` is False, if this method returns due to a timeout or with `wait=False` and an etag indicating an
            unchanged value, or True, if due to a new value. Should be used for the HTTP status code: 200 vs. 304.
            `value` represents the new value (or None when `changed=False`).
            `etag` is the id of the new future. It can be used as the HTML `ETag` header, so the client can send it in
            the `If-None-Match` header of the next request, which is passed to this method's `etag_match` parameter.
        """
        # If not waiting for next value and etag indicates unchanged value: return with `changed=False`
        if not wait and etag_match == str(id(self.future)):
            return False, None, str(id(self.future))
        # If not waiting for next value *or* etag indicates changed value: return current value
        if not wait or (etag_match is not None and etag_match != id(self.future)):
            value = await self._from_provider()
            return True, value, str(id(self.future))

        # If waiting for next value: Await future using timeout
        try:
            value = await asyncio.wait_for(self.future, timeout=timeout)
            return True, value, str(id(self.future))
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return False, None, str(id(self.future))


from . import widgets
