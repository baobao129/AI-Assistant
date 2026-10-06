from concurrent.futures import ThreadPoolExecutor

import pymysql
from pymilvus import MilvusClient

from backend.infrastructure.config import Settings


class DependencyHealthProbe:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _mysql(self) -> str:
        try:
            with (
                pymysql.connect(
                    host=self.settings.mysql_host,
                    port=self.settings.mysql_port,
                    user=self.settings.mysql_user,
                    password=self.settings.mysql_password.get_secret_value(),
                    database=self.settings.mysql_database,
                    connect_timeout=3,
                    read_timeout=3,
                    write_timeout=3,
                ) as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute("SELECT 1")
                return "healthy" if cursor.fetchone() == (1,) else "unavailable"
        except Exception:
            return "unavailable"

    def _milvus(self) -> str:
        client = None
        try:
            client = MilvusClient(uri=self.settings.milvus_uri, timeout=3)
            client.list_collections(timeout=3)
            return "healthy"
        except Exception:
            return "unavailable"
        finally:
            if client is not None:
                client.close()

    def check(self) -> dict[str, str]:
        with ThreadPoolExecutor(max_workers=2) as pool:
            mysql = pool.submit(self._mysql)
            milvus = pool.submit(self._milvus)
            return {"mysql": mysql.result(), "milvus": milvus.result()}
