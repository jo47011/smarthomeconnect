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
import enum
import functools
import logging
import signal
from typing import Set, NamedTuple, Dict, Any

from .timer import timer_supervisor
from .variables import read_initialize_variables

logger = logging.getLogger(__name__)

_REGISTERED_INTERFACES: Set["AbstractInterface"] = set()

event_loop = asyncio.get_event_loop()
_SHC_STOPPED = asyncio.Event(loop=event_loop)
_SHC_STOPPED.set()


class AbstractInterface(metaclass=abc.ABCMeta):
    def __init__(self):
        register_interface(self)

    @abc.abstractmethod
    async def start(self) -> None:
        """
        TODO
        :return:
        """
        pass

    @abc.abstractmethod
    async def stop(self) -> None:
        """
        TODO
        :return:
        """
        pass

    async def get_status(self) -> "InterfaceStatus":
        """
        TODO
        :return:
        """
        return InterfaceStatus()


class Status(enum.Enum):
    OK = 0
    WARNING = 1
    CRITICAL = 2
    UNKNOWN = 3


class InterfaceStatus(NamedTuple):
    status: Status = Status.OK
    message: str = ""
    indicators: Dict[str, Any] = {}


def register_interface(interface: AbstractInterface):
    _REGISTERED_INTERFACES.add(interface)


async def run():
    _SHC_STOPPED.clear()
    logger.info("Starting up interfaces ...")
    await asyncio.gather(*(interface.start() for interface in _REGISTERED_INTERFACES))
    logger.info("All interfaces started successfully. Initializing variables ...")
    await read_initialize_variables()
    logger.info("Variables initialized successfully. Starting timers ...")
    await timer_supervisor.start()
    logger.info("Timers initialized successfully. SHC startup finished.")
    # Now, keep this task awaiting until SHC is stopped via stop()
    await _SHC_STOPPED.wait()


async def stop():
    logger.info("Shutting down interfaces ...")
    await asyncio.gather(*(interface.stop() for interface in _REGISTERED_INTERFACES), timer_supervisor.stop(),
                         return_exceptions=True)
    _SHC_STOPPED.set()


def handle_signal(sig: int, loop: asyncio.AbstractEventLoop):
    logger.info("Got signal {}. Initiating shutdown ...".format(sig))
    loop.create_task(stop())


def main():
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        event_loop.add_signal_handler(sig, functools.partial(handle_signal, sig, event_loop))
    event_loop.run_until_complete(run())
