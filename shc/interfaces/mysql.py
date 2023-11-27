import asyncio
import datetime
import enum
import json
import logging
from typing import Optional, Type, Generic, List, Tuple, Any, Dict, Callable

import aiomysql
import pymysql

from shc.base import T, Readable, UninitializedError
from shc.conversion import SHCJsonEncoder, from_json
from shc.data_logging import WritableDataLogVariable
from shc.interfaces._helper import ReadableStatusInterface
from shc.supervisor import InterfaceStatus, ServiceStatus

logger = logging.getLogger(__name__)


class MySQLConnector(ReadableStatusInterface):
    def __init__(self, log_table: str = "log", **kwargs):
        super().__init__()
        # see https://aiomysql.readthedocs.io/en/latest/connection.html#connection for valid parameters
        self.connect_args = kwargs
        self.pool: Optional[aiomysql.Pool] = None
        self.pool_ready = asyncio.Event()
        self.variables: Dict[str, MySQLPersistenceVariable] = {}
        self.log_table = log_table

    async def start(self) -> None:
        logger.info("Creating MySQL connection pool ...")
        self.pool = await aiomysql.create_pool(**self.connect_args)
        self.pool_ready.set()

    async def stop(self) -> None:
        if self.pool is not None:
            logger.info("Closing all MySQL connections ...")
            self.pool.close()
            await self.pool.wait_closed()

    async def _get_status(self) -> "InterfaceStatus":
        if not self.pool_ready.is_set():
            return InterfaceStatus(ServiceStatus.CRITICAL, "Interface not started yet")
        assert isinstance(self.pool, aiomysql.Pool)
        try:
            async with self.pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT * from `log` WHERE FALSE")
        except pymysql.err.MySQLError as e:
            return InterfaceStatus(ServiceStatus.CRITICAL, str(e))
        return InterfaceStatus(ServiceStatus.OK, "")

    def variable(self, type_: Type[T], name: str, log: bool = True) -> "MySQLPersistenceVariable[T]":
        # TODO implement non-logging MySQL variables
        if not log:
            raise NotImplementedError()
        if name in self.variables:
            variable = self.variables[name]
            if variable.type is not type_:
                raise ValueError("MySQL persistence variable with name {} has already been defined with type {}"
                                 .format(name, variable.type.__name__))
            return variable
        else:
            variable = MySQLPersistenceVariable(self, type_, name, self.log_table)
            self.variables[name] = variable
            return variable

    def __repr__(self) -> str:
        return "{}({})".format(self.__class__.__name__, {k: v for k, v in self.connect_args.items()
                                                         if k in ('host', 'db', 'port', 'unix_socket')})


class MySQLPersistenceVariable(WritableDataLogVariable[T], Readable[T], Generic[T]):
    type: Type[T]

    def __init__(self, interface: MySQLConnector, type_: Type[T], name: str, table: str = "log"):
        self.type = type_
        super().__init__()
        self.interface = interface
        self.name = name
        self.table = table
        self._insert_query = self._get_insert_query()
        self._retrieve_query = self._get_retrieve_query(include_previous=False)
        self._retrieve_with_prev_query = self._get_retrieve_query(include_previous=True)
        self._read_query = self._get_read_query()
        self._to_mysql_converter: Callable[[T], Any] = self._get_to_mysql_converter(type_)
        self._from_mysql_converter: Callable[[Any], T] = self._get_from_mysql_converter(type_)

    async def read(self) -> T:
        await self.interface.pool_ready.wait()
        assert self.interface.pool is not None
        async with self.interface.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(self._read_query, {'name': self.name})
                value = await cur.fetchone()
        if value is None:
            raise UninitializedError("No value has been persisted in MySQL database yet")
        logger.debug("Retrieved value %s for %s from %s", value, self, self.interface)
        return self._from_mysql_converter(value[0])

    async def _write_to_data_log(self, values: List[Tuple[datetime.datetime, T]]) -> None:
        await self.interface.pool_ready.wait()
        assert self.interface.pool is not None
        async with self.interface.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(self._insert_query,
                                      [{'ts': ts.astimezone(datetime.timezone.utc),
                                        'value': self._to_mysql_converter(value),
                                        'name': self.name}
                                       for ts, value in values])
            await conn.commit()

    async def retrieve_log(self, start_time: datetime.datetime, end_time: datetime.datetime,
                           include_previous: bool = True) -> List[Tuple[datetime.datetime, T]]:
        await self.interface.pool_ready.wait()
        assert self.interface.pool is not None
        async with self.interface.pool.acquire() as conn:
            async with conn.cursor() as cur:
                if include_previous:
                    await cur.execute(
                        self._retrieve_with_prev_query,
                        {'name': self.name,
                         'start': start_time.astimezone(datetime.timezone.utc),
                         'end': end_time.astimezone(datetime.timezone.utc)})
                else:
                    await cur.execute(self._retrieve_query,
                                      {'name': self.name,
                                       'start': start_time.astimezone(datetime.timezone.utc),
                                       'end': end_time.astimezone(datetime.timezone.utc)})

                return [(row[0].replace(tzinfo=datetime.timezone.utc), self._from_mysql_converter(row[1]))
                        for row in await cur.fetchall()]

    def _get_insert_query(self) -> str:
        return f"INSERT INTO `{self.table}` (`name`, `ts`, `{self._type_to_column(self.type)}`) " \
               f"VALUES (%(name)s, %(ts)s, %(value)s)"

    def _get_retrieve_query(self, include_previous: bool) -> str:
        if include_previous:
            return f"(SELECT `ts`, `{self._type_to_column(self.type)}` " \
                   f" FROM `{self.table}` " \
                   f" WHERE `name` = %(name)s AND `ts` < %(start)s " \
                   f" ORDER BY `ts` DESC LIMIT 1) " \
                   f"UNION (SELECT `ts`, `{self._type_to_column(self.type)}` " \
                   f"       FROM `{self.table}` " \
                   f"       WHERE `name` = %(name)s AND `ts` >= %(start)s AND `ts` < %(end)s " \
                   f"       ORDER BY `ts` ASC)"
        else:
            return f"SELECT `ts`, `{self._type_to_column(self.type)}` " \
                   f"FROM `{self.table}` " \
                   f"WHERE `name` = %(name)s AND `ts` >= %(start)s AND `ts` < %(end)s " \
                   f"ORDER BY `ts` ASC"

    def _get_read_query(self) -> str:
        return f"SELECT `{self._type_to_column(self.type)}` " \
               f"FROM `{self.table}` " \
               f"WHERE `name` = %(name)s " \
               f"ORDER BY `ts` DESC LIMIT 1"

    @classmethod
    def _type_to_column(cls, type_: Type) -> str:
        if issubclass(type_, (int, bool)):
            return 'value_int'
        elif issubclass(type_, float):
            return 'value_float'
        elif issubclass(type_, str):
            return 'value_str'
        elif issubclass(type_, enum.Enum):
            return cls._type_to_column(type(next(iter(type_.__members__.values())).value))
        else:
            return 'value_str'

    @staticmethod
    def _get_to_mysql_converter(type_: Type[T]) -> Callable[[T], Any]:
        if type_ in (bool, int, float, str) or issubclass(type_, bool):
            return lambda x: x
        elif isinstance(type_, int):
            return lambda value: int(value)
        elif isinstance(type_, float):
            return lambda value: float(value)
        elif isinstance(type_, str):
            return lambda value: str(value)
        elif issubclass(type_, enum.Enum):
            return lambda value: value.value
        else:
            return lambda value: json.dumps(value, cls=SHCJsonEncoder)

    @staticmethod
    def _get_from_mysql_converter(type_: Type[T]) -> Callable[[Any], T]:
        if type_ in (bool, int, float, str):
            return lambda x: x
        elif issubclass(type_, (bool, int, float, str, enum.Enum)):
            return lambda value: type_(value)
        else:
            return lambda value: from_json(type_, json.loads(value))

    def __repr__(self):
        return "<{} '{}'>".format(self.__class__.__name__, self.name)
