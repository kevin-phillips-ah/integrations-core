# (C) Datadog, Inc. 2020-present
# All rights reserved
# Licensed under a 3-clause BSD style license (see LICENSE)
import threading
from contextlib import closing

import snowflake.connector as sf
from snowflake.connector.network import SnowflakeRestful

from datadog_checks.base import AgentCheck, ConfigurationError
from datadog_checks.base.utils.db import QueryManager

from . import queries
from .config import Config

METRIC_GROUPS = {
    'snowflake.query': [queries.WarehouseLoad, queries.QueryHistory],
    'snowflake.billing': [queries.CreditUsage, queries.WarehouseCreditUsage],
    'snowflake.storage': [queries.StorageUsageMetrics],
    'snowflake.storage.database': [queries.DatabaseStorageMetrics],
    'snowflake.storage.table': [queries.TableStorage],
    'snowflake.logins': [queries.LoginMetrics],
    'snowflake.data_transfer': [queries.DataTransferHistory],
    'snowflake.auto_recluster': [queries.AutoReclusterHistory],
    'snowflake.pipe': [queries.PipeHistory],
    'snowflake.replication': [queries.ReplicationUsage],
}


class SnowflakeCheck(AgentCheck):
    """
    Collect Snowflake account usage metrics
    """

    __NAMESPACE__ = 'snowflake'

    SERVICE_CHECK_CONNECT = 'snowflake.can_connect'

    MONKEY_PATCH_LOCK = threading.Lock()

    def __init__(self, *args, **kwargs):
        super(SnowflakeCheck, self).__init__(*args, **kwargs)
        self.config = Config(self.instance)
        self._conn = None

        # Add default tags like account to all metrics
        self._tags = self.config.tags + ['account:{}'.format(self.config.account)]

        if self.config.password:
            self.register_secret(self.config.password)

        if self.config.role == 'ACCOUNTADMIN':
            self.log.info(
                'Snowflake `role` is set as `ACCOUNTADMIN` which should be used cautiously, '
                'refer to docs about custom roles.'
            )

        self.metric_queries = []
        self.errors = []
        for mgroup in self.config.metric_groups:
            try:
                self.metric_queries.extend(METRIC_GROUPS[mgroup])
            except KeyError:
                self.errors.append(mgroup)

        if self.errors:
            self.log.warning('Invalid metric_groups found in snowflake conf.yaml: %s', (', '.join(self.errors)))
        if not self.metric_queries:
            raise ConfigurationError('No valid metric_groups configured, please list at least one.')

        self._proxies = self.http.options['proxies']  # SKIP_HTTP_VALIDATION
        self._query_manager = QueryManager(self, self.execute_query_raw, queries=self.metric_queries, tags=self._tags)
        self.check_initializations.append(self._query_manager.compile_queries)

    def check(self, _):
        self.connect()

        if self._conn is not None:
            # Execute queries
            self._query_manager.execute()

            self._collect_version()

            self.log.debug("Closing connection to Snowflake...")
            self._conn.close()

    def execute_query_raw(self, query):
        """
        Executes query with timestamp from parts if comparing start_time field.
        """
        with closing(self._conn.cursor()) as cursor:
            cursor.execute(query)

            if cursor.rowcount is None or cursor.rowcount < 1:
                self.log.debug("Failed to fetch records from query: `%s`", query)
                return []
            return cursor.fetchall()

    def connect(self):
        self.log.debug(
            "Establishing a new connection to Snowflake: account=%s, user=%s, database=%s, schema=%s, warehouse=%s, "
            "role=%s, login_timeout=%s, authenticator=%s, ocsp_response_cache_filename=%s",
            self.config.account,
            self.config.user,
            self.config.database,
            self.config.schema,
            self.config.warehouse,
            self.config.role,
            self.config.login_timeout,
            self.config.authenticator,
            self.config.ocsp_response_cache_filename,
        )

        try:
            with self.MONKEY_PATCH_LOCK:
                # Monkey patch proxies to request_exec
                SnowflakeRestful._request_exec = self._make_snowflake_request_func(
                    self._proxies, SnowflakeRestful._request_exec
                )
                conn = sf.connect(
                    user=self.config.user,
                    password=self.config.password,
                    account=self.config.account,
                    database=self.config.database,
                    schema=self.config.schema,
                    warehouse=self.config.warehouse,
                    role=self.config.role,
                    passcode_in_password=self.config.passcode_in_password,
                    passcode=self.config.passcode,
                    client_prefetch_threads=self.config.client_prefetch_threads,
                    login_timeout=self.config.login_timeout,
                    ocsp_response_cache_filename=self.config.ocsp_response_cache_filename,
                    authenticator=self.config.authenticator,
                    token=self.config.token,
                    client_session_keep_alive=self.config.client_keep_alive,
                )
        except Exception as e:
            msg = "Unable to connect to Snowflake: {}".format(e)
            self.service_check(self.SERVICE_CHECK_CONNECT, self.CRITICAL, message=msg, tags=self._tags)
            self.warning(msg)
        else:
            self.service_check(self.SERVICE_CHECK_CONNECT, self.OK, tags=self._tags)
            self._conn = conn

    def _make_snowflake_request_func(self, proxies, method):
        """
        This is a workaround to include proxy config in the Snowflake connection.
        The current Snowflake logic applies global proxy configs via env vars.

        TODO: Remove when https://github.com/snowflakedb/snowflake-connector-python/pull/352 gets merged
        """

        def _request_exec(*args, **kwargs):
            session = kwargs.get('session') or args[1]
            session.proxies = proxies
            try:
                return method(*args, **kwargs)
            except Exception as e:
                msg = "Encountered error while attempting to connect to Snowflake "
                if proxies:
                    self.log.error("%s via proxy settings: %s", msg, str(e))
                else:
                    self.log.error("%s: %s", msg, str(e))
                return

        return _request_exec

    @AgentCheck.metadata_entrypoint
    def _collect_version(self):
        try:
            raw_version = self.execute_query_raw("select current_version();")
            version = raw_version[0][0]
        except Exception as e:
            self.log.error("Error collecting version for Snowflake: %s", e)
        else:
            if version:
                self.set_metadata('version', version)
