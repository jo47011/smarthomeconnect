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
_EXIT_CODE = 0
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


async def interface_failure(interface_name: str = "n/a") -> None:
    """
    Shut down the SHC application due to a critical error in an interface

    This coroutine shall be called from an interface's background Task on a critical failure.
    It will shut down the SHC system gracefully and lets the Python process return with exit code 1.

    :param interface_name: String identifying the interface which caused the shutdown.
    """
    logger.warning("Shutting down SHC due to error in interface %s", interface_name)
    global _EXIT_CODE
    _EXIT_CODE = 1
    asyncio.create_task(stop())


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


def main() -> int:
    """
    Main entry point for running an SHC application

    This function starts an asyncio event loop to run the timers and interfaces. It registers signal handlers for
    SIGINT, SIGTERM, and SIGHUP to shut down all interfaces gracefully when such a signal is received. The `main`
    function blocks until shutdown is completed and returns the exit code. Thus, it should be used with
    :func:`sys.exit`::

        import sys
        import shc

        # setup interfaces, connect objects, etc.

        sys.exit(shc.main())

    A shutdown can also be triggered by a critical error in an interface (indicated via :func:`interface_failure`), in
    which case the the exit code will be != 0.

    :return: application exit code to be passed to sys.exit()
    """
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        event_loop.add_signal_handler(sig, functools.partial(handle_signal, sig, event_loop))
    event_loop.run_until_complete(run())
    return _EXIT_CODE
