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
import json
import datetime
import logging
import math
from typing import Type, Generic, List, Any, Optional, Set, Tuple, Union, cast, TypeVar, Callable, Sequence

import aiohttp.web

from .. import timer
from ..base import T, Readable, Writable, UninitializedError
from ..conversion import SHCJsonEncoder
from ..web.interface import WebUIConnector

logger = logging.getLogger(__name__)


class AggregationMethod(enum.Enum):
    AVERAGE = 0
    MINIMUM = 1
    MAXIMUM = 2
    ON_TIME = 3
    ON_TIME_RATIO = 4


class DataLogVariable(Generic[T], metaclass=abc.ABCMeta):
    type: Type[T]

    @abc.abstractmethod
    async def retrieve_log(self, start_time: datetime.datetime, end_time: datetime.datetime,
                           include_previous: bool = True) -> List[Tuple[datetime.datetime, T]]:
        """
        Retrieve all log entries for this log variable in the specified time range from the log backend/database

        The method shall return a list of all log entries with a timestamp greater or equal to the `start_time` and
        less than the `end_time`. If `include_previous` is True (shall be the default value), the last entry *before*
        the start shall also be included, if there is no entry exactly at the start_time.

        :param start_time: Begin of the time range (inclusive)
        :param end_time: End of the time range (exclusive)
        :param include_previous: If True (the default), the last value *before* `start_time`
        """
        pass

    async def retrieve_aggregated_log(self, start_time: datetime.datetime, end_time: datetime.datetime,  # noqa: C901
                                      aggregation_method: AggregationMethod, aggregation_interval: datetime.timedelta
                                      ) -> List[Tuple[datetime.datetime, float]]:
        data = await self.retrieve_log(start_time, end_time, include_previous=True)
        # TODO run aggregation in thread pool?
        return aggregate(data, self.type, start_time, end_time, aggregation_method, aggregation_interval)


class WritableDataLogVariable(Writable[T], DataLogVariable[T], Generic[T], metaclass=abc.ABCMeta):
    type: Type[T]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._data_log_subscribers: List["LiveDataLogView"] = []
        self._pending_values: List[Tuple[datetime.datetime, T]] = []
        self._mutex = asyncio.Lock()
        #: event to track running queue flushes: If the event is present, a concurrent task is waiting to do a
        #: flush and will set the event as soon as the values, which are now added to the queue are flushed
        self._flushing_finished: Optional[asyncio.Event] = None

    async def _write(self, value: T, origin: List[Any]) -> None:
        self._pending_values.append((datetime.datetime.now(datetime.timezone.utc), value))
        if self._flushing_finished is None:
            # We will do the flush in this task
            self._flushing_finished = asyncio.Event()
            # TODO we could sleep for some time based on the last flush timestamp (but listen for shutdown events?) here
            #  to ensure a maximum update rate by gathering multiple values into a single flush
            async with self._mutex:
                # atomic retrieval + clear of the _pending_values queue
                flushed_values = self._pending_values
                self._pending_values = []
                current_finish_event = self._flushing_finished
                self._flushing_finished = None
                # in parallel: Write new values to data log backend and subscribers
                tasks = [self._write_to_data_log(flushed_values)] + [subscriber._new_log_values_written(flushed_values)
                                                                     for subscriber in self._data_log_subscribers]
                if len(tasks) == 1:
                    await tasks[0]
                else:
                    await asyncio.gather(*tasks)
                current_finish_event.set()
        else:
            # Some other task is currently waiting for the flush, let's wait for it to finish
            current_finish_event = self._flushing_finished
            await current_finish_event.wait()

    @abc.abstractmethod
    async def _write_to_data_log(self, values: List[Tuple[datetime.datetime, T]]) -> None:
        # TODO comment: timestamps are timezone-aware in UTC
        raise NotImplementedError()

    async def retrieve_log_sync(self, start_time: datetime.datetime, end_time: datetime.datetime,
                                include_previous: bool = True) -> List[Tuple[datetime.datetime, T]]:
        async with self._mutex:
            return await self.retrieve_log(start_time, end_time, include_previous)

    def subscribe_data_log(self, subscriber: "LiveDataLogView") -> None:
        self._data_log_subscribers.append(subscriber)


class LiveDataLogView(Generic[T], metaclass=abc.ABCMeta):
    """
    :param data_log:
    :param interval:
    :param external_updates:
    :param update_interval:
    """

    def __init__(self, data_log: DataLogVariable[T],
                 interval: datetime.timedelta,
                 aggregation: Optional[AggregationMethod] = None,
                 aggregation_interval: Optional[datetime.timedelta] = None,
                 align_to: datetime.datetime = datetime.datetime(2020, 1, 1, 0, 0, 0),
                 update_interval: Optional[datetime.timedelta] = None,
                 external_updates: bool = False,
                 **kwargs):
        super().__init__(**kwargs)
        self.data_log = data_log
        self.interval = interval
        self.external_updates = external_updates
        self.aggregation = aggregation
        self.aggregation_interval = aggregation_interval
        if self.aggregation is not None and self.aggregation_interval is None:
            raise ValueError("aggregation_interval must be given if aggregation is enabled")
        self.align_to = align_to
        #: Whether we use push updates of the values via subscription of a WritableDataLogVariable (True) or
        #: interval-based update via log retrieval (potentially also triggered via subscription) (False)
        self.push = isinstance(data_log, WritableDataLogVariable) and not external_updates and aggregation is None
        if isinstance(self.data_log, WritableDataLogVariable):
            self.data_log.subscribe_data_log(self)
        if not self.push:
            update_interval = (update_interval if update_interval is not None
                               else min(interval / 20, datetime.timedelta(minutes=1)))
            self.timer = timer.Every(update_interval)
            self.timer.trigger(self._update, synchronous=True)

        #: In case of interval-based update operation: Store the end-time of the last update
        self._last_retrieved_timestamp: Optional[datetime.datetime] = None
        #: Mutex to ensure serialization of updates (incl. modification of _last_retrieved_timestamp) to avoid
        #: double-processing of log entries in case of interval-based update operation
        self._mutex = asyncio.Lock()

    async def _new_log_values_written(self, values: List[Tuple[datetime.datetime, T]]) -> None:
        if self.push:
            await self._process_new_logvalues(values)
        else:
            asyncio.create_task(self._update())

    async def _update(self, *_args) -> None:
        async with self._mutex:
            begin_time, end_time = self._data_retrieval_interval(True)
            data: Sequence[Tuple[datetime.datetime, Union[T, float]]]
            if self.aggregation is not None:
                assert self.aggregation_interval is not None
                data = await self.data_log.retrieve_aggregated_log(begin_time, end_time, self.aggregation,
                                                                   self.aggregation_interval)
            else:
                data = await self.data_log.retrieve_log(begin_time, end_time, include_previous=False)
            await self._process_new_logvalues(data)
            self._last_retrieved_timestamp = end_time

    async def get_current_view(self, include_previous: bool = False) -> Sequence[Tuple[datetime.datetime, Union[T, float]]]:
        if self.push:
            # With push updates, we need to use the synchronized retrieve_log() method to avoid losing
            # new values during the retrieval
            assert isinstance(self.data_log, WritableDataLogVariable)
            begin_time, end_time = self._data_retrieval_interval(False)
            return await self.data_log.retrieve_log_sync(begin_time,
                                                         end_time,
                                                         include_previous=include_previous)
        else:
            async with self._mutex:
                begin_time, end_time = self._data_retrieval_interval(False)
                if self.aggregation is not None:
                    assert self.aggregation_interval is not None
                    return await self.data_log.retrieve_aggregated_log(begin_time, end_time, self.aggregation,
                                                                       self.aggregation_interval)
                else:
                    return await self.data_log.retrieve_log(
                        datetime.datetime.now(datetime.timezone.utc) - self.interval,
                        end_time,
                        include_previous=include_previous)

    def _data_retrieval_interval(self, for_update: bool) -> Tuple[datetime.datetime, datetime.datetime]:
        """
        Calculate the begin and end timestamps to be passed to :meth:`PersistenceVariable.retrieve_aggregated_log` based
        on the configured interval, aggregation_interval, align timestamp.

        TODO: For interval-based update (incl. aggregated view) important to do this in _mutex

        :return: (start_time, end_time) for `retrieve_log()` resp. `retrieve_aggregated_log()`
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        if self.push:
            assert not for_update
            return now - self.interval, now

        if not for_update and self._last_retrieved_timestamp is not None:
            # We need to track make sure that all our clients are have the same latest data, so that the next
            # regular interval update (or push update if available) will not duplicate data. Thus, we do not
            # fetch the data up to now but only up to the latest log entry that is known to all other clients.
            end = self._last_retrieved_timestamp
        else:
            end = now

        if self.aggregation is None:
            if for_update:
                begin = self._last_retrieved_timestamp if self._last_retrieved_timestamp else end - self.interval
            else:
                begin = now - self.interval
        else:
            assert self.aggregation_interval is not None
            preliminary_begin = now - self.interval
            if for_update and self._last_retrieved_timestamp is not None:
                preliminary_begin = self._last_retrieved_timestamp - self.aggregation_interval
            align = self.align_to.astimezone()
            align_count = (preliminary_begin - align) // self.aggregation_interval
            begin = align + (align_count + 1) * self.aggregation_interval

        return begin, end

    @abc.abstractmethod
    async def _process_new_logvalues(self, values: Sequence[Tuple[datetime.datetime, Union[T, float]]]) -> None:
        # TODO warning: do not use _get_current_view here
        raise NotImplementedError()


class LoggingWebUIView(LiveDataLogView[T], WebUIConnector, Generic[T]):
    """
    A WebUIConnector which subclasses LiveDataLogView to allow retrieving and live-update of a DataLogVariable
    for a specified time interval and via the Webinterface UI websocket.
    """

    def __init__(self, data_log: DataLogVariable[T],
                 interval: datetime.timedelta,
                 aggregation: Optional[AggregationMethod] = None,
                 aggregation_interval: Optional[datetime.timedelta] = None,
                 align_to: datetime.datetime = datetime.datetime(2020, 1, 1, 0, 0, 0),
                 update_interval: Optional[datetime.timedelta] = None,
                 external_updates: bool = False,
                 converter: Optional[Callable[[T], Any]] = None,
                 include_previous: bool = True):
        super().__init__(data_log=data_log, interval=interval, aggregation=aggregation,
                         aggregation_interval=aggregation_interval, align_to=align_to, update_interval=update_interval,
                         external_updates=external_updates)
        self.converter: Callable = converter or (lambda x: x)
        self.include_previous = include_previous

    async def _process_new_logvalues(self, values: Sequence[Tuple[datetime.datetime, Union[T, float]]]) -> None:
        await self._websocket_publish({
            'init': False,
            'data': [(timestamp, self.converter(value))
                     for (timestamp, value) in values]
        })

    async def _websocket_before_subscribe(self, ws: aiohttp.web.WebSocketResponse) -> None:
        data = await self.get_current_view(self.include_previous)
        await ws.send_str(json.dumps({'id': id(self),
                                      'v': {
                                          'init': True,
                                          'data': [(ts, self.converter(v)) for ts, v in data]
                                      }},
                                     cls=SHCJsonEncoder))


def aggregate(data: List[Tuple[datetime.datetime, T]], type: Type[T], start_time: datetime.datetime,
              end_time: datetime.datetime,
              aggregation_method: AggregationMethod, aggregation_interval: datetime.timedelta):
    aggregation_timestamps = [start_time + i * aggregation_interval
                              for i in range(math.ceil((end_time - start_time) / aggregation_interval))]

    # The last aggregation_timestamps is not added to the results, but only used to delimit the last aggregation
    # interval.
    if aggregation_timestamps[-1] < end_time:
        aggregation_timestamps.append(end_time)

    if aggregation_method in (AggregationMethod.MINIMUM, AggregationMethod.MAXIMUM, AggregationMethod.AVERAGE):
        if not issubclass(type, (int, float)):
            raise TypeError("min/max/avg. aggregation is only applicable to int and float type log variables.")

    elif aggregation_method not in (AggregationMethod.ON_TIME, AggregationMethod.ON_TIME_RATIO):
        raise ValueError("Unsupported aggregation method {}".format(aggregation_method))

    aggregator_t = {
        AggregationMethod.MINIMUM: MinAggregator,
        AggregationMethod.MAXIMUM: MaxAggregator,
        AggregationMethod.AVERAGE: AverageAggregator,
        AggregationMethod.ON_TIME: OnTimeAggregator,
        AggregationMethod.ON_TIME_RATIO: OnTimeRatioAggregator,
    }[aggregation_method]()  # type: ignore
    aggregator = cast(AbstractAggregator[T, Any], aggregator_t)  # This is guaranteed by our type checks above

    next_aggr_ts_index = 0
    # Get first entry and its timestamp for skipping of empty aggregation intervals and initialization of the
    # first relevant interval
    iterator = iter(data)
    try:
        last_ts, last_value = next(iterator)
    except StopIteration:
        return []

    # Ignore aggregation intervals before the first entry
    while last_ts >= aggregation_timestamps[next_aggr_ts_index]:
        next_aggr_ts_index += 1
        if next_aggr_ts_index >= len(aggregation_timestamps):
            return []

    result: List[Tuple[datetime.datetime, float]] = []
    aggregator.reset()

    for ts, value in iterator:
        # The timestamp is after the next aggregation timestamp, finalize the current aggregation timestamp
        # value
        if ts >= aggregation_timestamps[next_aggr_ts_index]:
            if next_aggr_ts_index > 0:
                # Add remaining part to the last aggregation interval
                aggregator.aggregate(last_ts, aggregation_timestamps[next_aggr_ts_index], last_value)

                # Add average result entry from accumulated values
                result.append((aggregation_timestamps[next_aggr_ts_index - 1], aggregator.get()))

            next_aggr_ts_index += 1
            if next_aggr_ts_index >= len(aggregation_timestamps):
                return result

            # Do the same for every following empty interval
            while ts >= aggregation_timestamps[next_aggr_ts_index]:
                aggregator.reset()
                aggregator.aggregate(aggregation_timestamps[next_aggr_ts_index - 1],
                                     aggregation_timestamps[next_aggr_ts_index], last_value)
                result.append((aggregation_timestamps[next_aggr_ts_index - 1], aggregator.get()))

                next_aggr_ts_index += 1
                if next_aggr_ts_index >= len(aggregation_timestamps):
                    return result

            aggregator.reset()

        # Accumulate the weighted value and time interval to the `*_sum` variables
        if next_aggr_ts_index > 0:
            interval_start = aggregation_timestamps[next_aggr_ts_index - 1]
            aggregator.aggregate(max(last_ts, interval_start), ts, last_value)

        last_value = value
        last_ts = ts

    if next_aggr_ts_index > 0:
        # Add remaining part to the last aggregation interval
        aggregator.aggregate(last_ts, aggregation_timestamps[next_aggr_ts_index], last_value)

        # Add average result entry from accumulated values
        result.append((aggregation_timestamps[next_aggr_ts_index - 1], aggregator.get()))

    next_aggr_ts_index += 1

    # Fill remaining aggregation intervals
    while next_aggr_ts_index < len(aggregation_timestamps):
        aggregator.reset()
        aggregator.aggregate(aggregation_timestamps[next_aggr_ts_index - 1],
                             aggregation_timestamps[next_aggr_ts_index], last_value)
        result.append((aggregation_timestamps[next_aggr_ts_index - 1], aggregator.get()))

        next_aggr_ts_index += 1
    return result


S = TypeVar('S')


class AbstractAggregator(Generic[T, S], metaclass=abc.ABCMeta):
    """
    Abstract base class for Aggregator classes, a special helper class, used to define the actual aggregation logic in
    :meth:`PersistenceVariable.retrieve_aggregated_log`. For each aggregation method (see :class:`AggregationMethods`),
    a specialized Aggregator class is available.

    The aggregator defines, how individual values, which lasted for a given time interval, are aggregated into a single
    value. It is controlled by the `retrieve_aggregated_log()` method via three interface methods:
    * :meth:`reset` is called at the begin of every aggregation interval to reset the aggregated value
    * :meth:`aggregate` is called for each value change with the value and the begin and end timestamps of the interval
        for which the value lasted. These time intervals can be used to calculate the value's weight in a weighted
        average or the on time of a bool aggregation
    * :meth:`get` is called at the end of every aggregation interval to retrieve the aggregated value

    """
    __slots__ = ()

    def reset(self) -> None:
        pass

    @abc.abstractmethod
    def get(self) -> S:
        pass

    @abc.abstractmethod
    def aggregate(self, start: datetime.datetime, end: datetime.datetime, value: T) -> None:
        pass


class AverageAggregator(AbstractAggregator[Union[float, int], float]):
    """
    Aggregator implementation for AggregationMethod.AVERAGE. It calculates an average of the values, weighted by their
    respective time intervals.
    """
    __slots__ = ('value_sum', 'time_sum')
    value_sum: float
    time_sum: float

    def reset(self) -> None:
        self.value_sum = 0.0
        self.time_sum = 0.0

    def get(self) -> float:
        return self.value_sum / self.time_sum

    def aggregate(self, start: datetime.datetime, end: datetime.datetime, value: Union[float, int]) -> None:
        delta_seconds = (end - start).total_seconds()
        self.time_sum += delta_seconds
        self.value_sum += value * delta_seconds


class MinAggregator(AbstractAggregator[Union[float, int], float]):
    """
    Aggregator implementation for AggregationMethod.MINIMUM. It calculates the minimum of all values in an aggregation
    interval. The value's begin..end interval is ignored. Thus, even values with 0 duration are taken into account.
    """
    __slots__ = ('value',)
    value: float

    def reset(self) -> None:
        self.value = float('inf')

    def get(self) -> float:
        return self.value

    def aggregate(self, start: datetime.datetime, end: datetime.datetime, value: Union[int, float]) -> None:
        self.value = min(self.value, value)


class MaxAggregator(AbstractAggregator[Union[float, int], float]):
    """
    Aggregator implementation for AggregationMethod.MAXIMUM. It calculates the maximum of all values in an aggregation
    interval. The value's begin..end interval is ignored. Thus, even values with 0 duration are taken into account.
    """
    __slots__ = ('value',)
    value: float

    def reset(self) -> None:
        self.value = float('-inf')

    def get(self) -> float:
        return self.value

    def aggregate(self, start: datetime.datetime, end: datetime.datetime, value: Union[int, float]) -> None:
        self.value = max(self.value, value)


class OnTimeAggregator(AbstractAggregator[Any, float]):
    """
    Aggregator implementation for AggregationMethod.ONTIME. It calculates the aggregated duration of all intervals where
    the value evaluates to True. The value is returned in seconds.
    """
    __slots__ = ('value',)
    value: datetime.timedelta

    def reset(self) -> None:
        self.value = datetime.timedelta(0)

    def get(self) -> float:
        return self.value.total_seconds()

    def aggregate(self, start: datetime.datetime, end: datetime.datetime, value: bool) -> None:
        if value:
            self.value += end - start


class OnTimeRatioAggregator(AbstractAggregator[Any, float]):
    """
    # Aggregator implementation for AggregationMethod.ONTIME. It calculates the ratio of all intervals where the value
    evaluates to True to the total duration of the aggregation interval.
    """
    __slots__ = ('value', 'total_time')
    value: datetime.timedelta
    total_time: datetime.timedelta

    def reset(self) -> None:
        self.value = datetime.timedelta(0)
        self.total_time = datetime.timedelta(0)

    def get(self) -> float:
        return self.value / self.total_time

    def aggregate(self, start: datetime.datetime, end: datetime.datetime, value: Any) -> None:
        delta = end - start
        self.total_time += delta
        if value:
            self.value += delta
