from multiprocessing import get_context
from unittest import mock

import agate
import decimal
import string
import random
import re
import pytest
import unittest
from unittest.mock import patch, MagicMock, create_autospec

import dbt_common.dataclass_schema
import dbt_common.exceptions.base

import dbt.adapters
from dbt.adapters.bigquery.relation_configs import PartitionConfig
from dbt.adapters.bigquery import BigQueryAdapter, BigQueryRelation
from google.cloud.bigquery.table import Table
from dbt.adapters.bigquery.connections import _sanitize_label, _VALIDATE_LABEL_LENGTH_LIMIT
from dbt_common.clients import agate_helper
import dbt_common.exceptions
from dbt.context.query_header import generate_query_header_context
from dbt.contracts.files import FileHash
from dbt.contracts.graph.manifest import ManifestStateCheck
from dbt.context.providers import RuntimeConfigObject, generate_runtime_macro_context

from google.cloud.bigquery import AccessEntry

from .utils import (
    config_from_parts_or_dicts,
    inject_adapter,
    TestAdapterConversions,
    load_internal_manifest_macros,
    mock_connection,
)


def _bq_conn():
    conn = MagicMock()
    conn.get.side_effect = lambda x: "bigquery" if x == "type" else None
    return conn


class BaseTestBigQueryAdapter(unittest.TestCase):
    def setUp(self):
        self.raw_profile = {
            "outputs": {
                "oauth": {
                    "type": "bigquery",
                    "method": "oauth",
                    "project": "dbt-unit-000000",
                    "schema": "dummy_schema",
                    "threads": 1,
                },
                "service_account": {
                    "type": "bigquery",
                    "method": "service-account",
                    "project": "dbt-unit-000000",
                    "schema": "dummy_schema",
                    "keyfile": "/tmp/dummy-service-account.json",
                    "threads": 1,
                },
                "external_oauth_wif": {
                    "type": "bigquery",
                    "method": "external-oauth-wif",
                    "project": "dbt-unit-000000",
                    "schema": "dummy_schema",
                    "threads": 1,
                    "token_endpoint": {
                        "type": "entra",
                        "request_url": "https://example.com/token",
                        "request_data": "mydata",
                    },
                    "audience": "https://example.com/audience",
                },
                "loc": {
                    "type": "bigquery",
                    "method": "oauth",
                    "project": "dbt-unit-000000",
                    "schema": "dummy_schema",
                    "threads": 1,
                    "location": "Luna Station",
                    "priority": "batch",
                    "maximum_bytes_billed": 0,
                },
                "api_endpoint": {
                    "type": "bigquery",
                    "method": "oauth",
                    "project": "dbt-unit-000000",
                    "schema": "dummy_schema",
                    "threads": 1,
                    "api_endpoint": "https://localhost:3001",
                },
                "impersonate": {
                    "type": "bigquery",
                    "method": "oauth",
                    "project": "dbt-unit-000000",
                    "schema": "dummy_schema",
                    "threads": 1,
                    "impersonate_service_account": "dummyaccount@dbt.iam.gserviceaccount.com",
                },
                "oauth-credentials-token": {
                    "type": "bigquery",
                    "method": "oauth-secrets",
                    "token": "abc",
                    "project": "dbt-unit-000000",
                    "schema": "dummy_schema",
                    "threads": 1,
                    "location": "Luna Station",
                    "priority": "batch",
                    "maximum_bytes_billed": 0,
                },
                "oauth-credentials": {
                    "type": "bigquery",
                    "method": "oauth-secrets",
                    "client_id": "abc",
                    "client_secret": "def",
                    "refresh_token": "ghi",
                    "token_uri": "jkl",
                    "project": "dbt-unit-000000",
                    "schema": "dummy_schema",
                    "threads": 1,
                    "location": "Luna Station",
                    "priority": "batch",
                    "maximum_bytes_billed": 0,
                },
                "oauth-no-project": {
                    "type": "bigquery",
                    "method": "oauth",
                    "schema": "dummy_schema",
                    "threads": 1,
                    "location": "Solar Station",
                },
                "dataproc-serverless-configured": {
                    "type": "bigquery",
                    "method": "oauth",
                    "schema": "dummy_schema",
                    "threads": 1,
                    "gcs_bucket": "dummy-bucket",
                    "compute_region": "europe-west1",
                    "submission_method": "serverless",
                    "dataproc_batch": {
                        "environment_config": {
                            "execution_config": {
                                "service_account": "dbt@dummy-project.iam.gserviceaccount.com",
                                "subnetwork_uri": "dataproc",
                                "network_tags": ["foo", "bar"],
                            }
                        },
                        "labels": {"dbt": "rocks", "number": "1"},
                        "runtime_config": {
                            "properties": {
                                "spark.executor.instances": "4",
                                "spark.driver.memory": "1g",
                            }
                        },
                    },
                },
                "dataproc-serverless-default": {
                    "type": "bigquery",
                    "method": "oauth",
                    "schema": "dummy_schema",
                    "threads": 1,
                    "gcs_bucket": "dummy-bucket",
                    "compute_region": "europe-west1",
                    "submission_method": "serverless",
                },
            },
            "target": "oauth",
        }

        self.project_cfg = {
            "name": "X",
            "version": "0.1",
            "project-root": "/tmp/dbt/does-not-exist",
            "profile": "default",
            "config-version": 2,
        }
        self.qh_patch = None

        @mock.patch("dbt.parser.manifest.ManifestLoader.build_manifest_state_check")
        def _mock_state_check(self):
            all_projects = self.all_projects
            return ManifestStateCheck(
                vars_hash=FileHash.from_contents("vars"),
                project_hashes={name: FileHash.from_contents(name) for name in all_projects},
                profile_hash=FileHash.from_contents("profile"),
            )

        self.load_state_check = mock.patch(
            "dbt.parser.manifest.ManifestLoader.build_manifest_state_check"
        )
        self.mock_state_check = self.load_state_check.start()
        self.mock_state_check.side_effect = _mock_state_check

    def tearDown(self):
        if self.qh_patch:
            self.qh_patch.stop()
        super().tearDown()

    def get_adapter(self, target) -> BigQueryAdapter:
        project = self.project_cfg.copy()
        profile = self.raw_profile.copy()
        profile["target"] = target
        config = config_from_parts_or_dicts(
            project=project,
            profile=profile,
        )
        adapter = BigQueryAdapter(config, get_context("spawn"))
        adapter.set_macro_resolver(load_internal_manifest_macros(config))
        adapter.set_macro_context_generator(generate_runtime_macro_context)
        adapter.connections.set_query_header(
            generate_query_header_context(config, adapter.get_macro_resolver())
        )

        self.qh_patch = patch.object(adapter.connections.query_header, "add")
        self.mock_query_header_add = self.qh_patch.start()
        self.mock_query_header_add.side_effect = lambda q: "/* dbt */\n{}".format(q)

        inject_adapter(adapter)
        return adapter


class TestBigQueryAdapterAcquire(BaseTestBigQueryAdapter):
    @patch(
        "dbt.adapters.bigquery.credentials._create_bigquery_defaults",
        return_value=("credentials", "project_id"),
    )
    @patch("dbt.adapters.bigquery.BigQueryConnectionManager.open", return_value=_bq_conn())
    def test_acquire_connection_oauth_no_project_validations(
        self, mock_open_connection, mock_get_bigquery_defaults
    ):
        adapter = self.get_adapter("oauth-no-project")
        mock_get_bigquery_defaults.assert_called_once()
        try:
            connection = adapter.acquire_connection("dummy")
            self.assertEqual(connection.type, "bigquery")

        except dbt_common.exceptions.base.DbtValidationError as e:
            self.fail("got DbtValidationError: {}".format(str(e)))

        except BaseException:
            raise

        mock_open_connection.assert_not_called()
        connection.handle
        mock_open_connection.assert_called_once()

    @patch("dbt.adapters.bigquery.BigQueryConnectionManager.open", return_value=_bq_conn())
    def test_acquire_connection_oauth_validations(self, mock_open_connection):
        adapter = self.get_adapter("oauth")
        try:
            connection = adapter.acquire_connection("dummy")
            self.assertEqual(connection.type, "bigquery")

        except dbt_common.exceptions.base.DbtValidationError as e:
            self.fail("got DbtValidationError: {}".format(str(e)))

        except BaseException:
            raise

        mock_open_connection.assert_not_called()
        connection.handle
        mock_open_connection.assert_called_once()

    @patch(
        "dbt.adapters.bigquery.credentials._create_bigquery_defaults",
        return_value=("credentials", "project_id"),
    )
    @patch(
        "dbt.adapters.bigquery.connections.BigQueryConnectionManager.open", return_value=_bq_conn()
    )
    def test_acquire_connection_dataproc_serverless(
        self, mock_open_connection, mock_get_bigquery_defaults
    ):
        adapter = self.get_adapter("dataproc-serverless-configured")
        mock_get_bigquery_defaults.assert_called_once()
        try:
            connection = adapter.acquire_connection("dummy")
            self.assertEqual(connection.type, "bigquery")

        except dbt_common.exceptions.ValidationException as e:
            self.fail("got ValidationException: {}".format(str(e)))

        except BaseException:
            raise

        mock_open_connection.assert_not_called()
        connection.handle
        mock_open_connection.assert_called_once()

    @patch("dbt.adapters.bigquery.BigQueryConnectionManager.open", return_value=_bq_conn())
    def test_acquire_connection_service_account_validations(self, mock_open_connection):
        adapter = self.get_adapter("service_account")
        try:
            connection = adapter.acquire_connection("dummy")
            self.assertEqual(connection.type, "bigquery")

        except dbt_common.exceptions.base.DbtValidationError as e:
            self.fail("got DbtValidationError: {}".format(str(e)))

        except BaseException:
            raise

        mock_open_connection.assert_not_called()
        connection.handle
        mock_open_connection.assert_called_once()

    @patch("dbt.adapters.bigquery.BigQueryConnectionManager.open", return_value=_bq_conn())
    def test_acquire_connection_oauth_token_validations(self, mock_open_connection):
        adapter = self.get_adapter("oauth-credentials-token")
        try:
            connection = adapter.acquire_connection("dummy")
            self.assertEqual(connection.type, "bigquery")

        except dbt_common.exceptions.base.DbtValidationError as e:
            self.fail("got DbtValidationError: {}".format(str(e)))

        except BaseException:
            raise

        mock_open_connection.assert_not_called()
        connection.handle
        mock_open_connection.assert_called_once()

    @patch("dbt.adapters.bigquery.BigQueryConnectionManager.open", return_value=_bq_conn())
    def test_acquire_connection_oauth_credentials_validations(self, mock_open_connection):
        adapter = self.get_adapter("oauth-credentials")
        try:
            connection = adapter.acquire_connection("dummy")
            self.assertEqual(connection.type, "bigquery")

        except dbt_common.exceptions.base.DbtValidationError as e:
            self.fail("got DbtValidationError: {}".format(str(e)))

        except BaseException:
            raise

        mock_open_connection.assert_not_called()
        connection.handle
        mock_open_connection.assert_called_once()

    @patch("dbt.adapters.bigquery.BigQueryConnectionManager.open", return_value=_bq_conn())
    def test_acquire_connection_impersonated_service_account_validations(
        self, mock_open_connection
    ):
        adapter = self.get_adapter("impersonate")
        try:
            connection = adapter.acquire_connection("dummy")
            self.assertEqual(connection.type, "bigquery")

        except dbt_common.exceptions.base.DbtValidationError as e:
            self.fail("got DbtValidationError: {}".format(str(e)))

        except BaseException:
            raise

        mock_open_connection.assert_not_called()
        connection.handle
        mock_open_connection.assert_called_once()

    @patch("dbt.adapters.bigquery.BigQueryConnectionManager.open", return_value=_bq_conn())
    def test_acquire_connection_external_oauth_wif_validations(self, mock_open_connection):
        adapter = self.get_adapter("external_oauth_wif")
        try:
            connection = adapter.acquire_connection("dummy")
            self.assertEqual(connection.type, "bigquery")

        except dbt_common.exceptions.base.DbtValidationError as e:
            self.fail("got DbtValidationError: {}".format(str(e)))

        except BaseException:
            raise

        mock_open_connection.assert_not_called()
        connection.handle
        mock_open_connection.assert_called_once()

    @patch("dbt.adapters.bigquery.BigQueryConnectionManager.open", return_value=_bq_conn())
    def test_acquire_connection_priority(self, mock_open_connection):
        adapter = self.get_adapter("loc")
        try:
            connection = adapter.acquire_connection("dummy")
            self.assertEqual(connection.type, "bigquery")
            self.assertEqual(connection.credentials.priority, "batch")

        except dbt_common.exceptions.base.DbtValidationError as e:
            self.fail("got DbtValidationError: {}".format(str(e)))

        mock_open_connection.assert_not_called()
        connection.handle
        mock_open_connection.assert_called_once()

    @patch("dbt.adapters.bigquery.BigQueryConnectionManager.open", return_value=_bq_conn())
    def test_acquire_connection_maximum_bytes_billed(self, mock_open_connection):
        adapter = self.get_adapter("loc")
        try:
            connection = adapter.acquire_connection("dummy")
            self.assertEqual(connection.type, "bigquery")
            self.assertEqual(connection.credentials.maximum_bytes_billed, 0)

        except dbt_common.exceptions.base.DbtValidationError as e:
            self.fail("got DbtValidationError: {}".format(str(e)))

        mock_open_connection.assert_not_called()
        connection.handle
        mock_open_connection.assert_called_once()

    def test_cancel_open_connections_empty(self):
        adapter = self.get_adapter("oauth")
        self.assertEqual(len(list(adapter.cancel_open_connections())), 0)

    def test_cancel_open_connections_master(self):
        adapter = self.get_adapter("oauth")
        key = adapter.connections.get_thread_identifier()
        adapter.connections.thread_connections[key] = mock_connection("master")
        self.assertEqual(len(list(adapter.cancel_open_connections())), 0)

    def test_cancel_open_connections_single(self):
        adapter = self.get_adapter("oauth")
        master = mock_connection("master")
        model = mock_connection("model")
        key = adapter.connections.get_thread_identifier()

        adapter.connections.thread_connections.update({key: master, 1: model})
        self.assertEqual(len(list(adapter.cancel_open_connections())), 1)

    @patch("dbt.adapters.bigquery.clients.ClientOptions")
    @patch("dbt.adapters.bigquery.credentials._create_bigquery_defaults")
    @patch("dbt.adapters.bigquery.clients.BigQueryClient")
    def test_location_user_agent(self, MockClient, mock_auth_default, MockClientOptions):
        creds = MagicMock()
        mock_auth_default.return_value = (creds, MagicMock())
        adapter = self.get_adapter("loc")

        connection = adapter.acquire_connection("dummy")
        mock_client_options = MockClientOptions.return_value

        MockClient.assert_not_called()
        connection.handle
        MockClient.assert_called_once_with(
            "dbt-unit-000000",
            creds,
            location="Luna Station",
            client_info=HasUserAgent(),
            client_options=mock_client_options,
        )

    @patch("dbt.adapters.bigquery.clients.ClientOptions")
    @patch("dbt.adapters.bigquery.credentials._create_bigquery_defaults")
    @patch("dbt.adapters.bigquery.clients.BigQueryClient")
    def test_api_endpoint_settable(self, MockClient, mock_auth_default, MockClientOptions):
        """Ensure api_endpoint is set on ClientOptions and passed to BigQueryClient."""

        creds = MagicMock()
        mock_auth_default.return_value = (creds, MagicMock())
        mock_client_options = MockClientOptions.return_value

        adapter = self.get_adapter("api_endpoint")
        connection = adapter.acquire_connection("dummy")
        MockClient.assert_not_called()
        connection.handle

        MockClientOptions.assert_called_once()
        kwargs = MockClientOptions.call_args.kwargs
        assert kwargs.get("api_endpoint") == "https://localhost:3001"

        MockClient.assert_called_once()
        assert MockClient.call_args.kwargs["client_options"] is mock_client_options


class HasUserAgent:
    PAT = re.compile(r"dbt-bigquery-\d+\.\d+\.\d+((a|b|rc)\d+)?")

    def __eq__(self, other):
        compare = getattr(other, "user_agent", "")
        return bool(self.PAT.match(compare))


class TestConnectionNamePassthrough(BaseTestBigQueryAdapter):
    def setUp(self):
        super().setUp()
        self._conn_patch = patch.object(BigQueryAdapter, "ConnectionManager")
        self.conn_manager_cls = self._conn_patch.start()

        self._relation_patch = patch.object(BigQueryAdapter, "Relation")
        self.relation_cls = self._relation_patch.start()

        self.mock_connection_manager = self.conn_manager_cls.return_value
        self.mock_connection_manager.get_if_exists().name = "mock_conn_name"
        self.conn_manager_cls.TYPE = "bigquery"
        self.relation_cls.get_default_quote_policy.side_effect = (
            BigQueryRelation.get_default_quote_policy
        )

        self.adapter = self.get_adapter("oauth")

    def tearDown(self):
        super().tearDown()
        self._conn_patch.stop()
        self._relation_patch.stop()

    def test_get_relation(self):
        self.adapter.get_relation("db", "schema", "my_model")
        self.mock_connection_manager.get_bq_table.assert_called_once_with(
            "db", "schema", "my_model"
        )

    @patch.object(BigQueryAdapter, "check_schema_exists")
    def test_drop_schema(self, mock_check_schema):
        mock_check_schema.return_value = True
        relation = BigQueryRelation.create(database="db", schema="schema")
        self.adapter.drop_schema(relation)
        self.mock_connection_manager.drop_dataset.assert_called_once_with("db", "schema")

    def test_get_columns_in_relation(self):
        self.mock_connection_manager.get_bq_table.side_effect = ValueError
        self.adapter.get_columns_in_relation(
            MagicMock(database="db", schema="schema", identifier="ident"),
        )
        self.mock_connection_manager.get_bq_table.assert_called_once_with(
            database="db", schema="schema", identifier="ident"
        )


class TestBigQueryRelation(unittest.TestCase):
    def setUp(self):
        pass

    def test_view_temp_relation(self):
        kwargs = {
            "type": None,
            "path": {"database": "test-project", "schema": "test_schema", "identifier": "my_view"},
            "quote_policy": {"identifier": False},
        }
        BigQueryRelation.validate(kwargs)

    def test_view_relation(self):
        kwargs = {
            "type": "view",
            "path": {"database": "test-project", "schema": "test_schema", "identifier": "my_view"},
            "quote_policy": {"identifier": True, "schema": True},
        }
        BigQueryRelation.validate(kwargs)

    def test_table_relation(self):
        kwargs = {
            "type": "table",
            "path": {
                "database": "test-project",
                "schema": "test_schema",
                "identifier": "generic_table",
            },
            "quote_policy": {"identifier": True, "schema": True},
        }
        BigQueryRelation.validate(kwargs)

    def test_external_source_relation(self):
        kwargs = {
            "type": "external",
            "path": {"database": "test-project", "schema": "test_schema", "identifier": "sheet"},
            "quote_policy": {"identifier": True, "schema": True},
        }
        BigQueryRelation.validate(kwargs)

    def test_invalid_relation(self):
        kwargs = {
            "type": "invalid-type",
            "path": {
                "database": "test-project",
                "schema": "test_schema",
                "identifier": "my_invalid_id",
            },
            "quote_policy": {"identifier": False, "schema": True},
        }
        with self.assertRaises(dbt_common.dataclass_schema.ValidationError):
            BigQueryRelation.validate(kwargs)


class TestBigQueryInformationSchema(unittest.TestCase):
    def setUp(self):
        pass

    def test_replace(self):
        kwargs = {
            "type": None,
            "path": {"database": "test-project", "schema": "test_schema", "identifier": "my_view"},
            # test for #2188
            "quote_policy": {"database": False},
            "include_policy": {
                "database": True,
                "schema": True,
                "identifier": True,
            },
        }
        BigQueryRelation.validate(kwargs)
        relation = BigQueryRelation.from_dict(kwargs)
        info_schema = relation.information_schema()

        tables_schema = info_schema.replace(information_schema_view="__TABLES__")
        assert tables_schema.information_schema_view == "__TABLES__"
        assert tables_schema.include_policy.schema is True
        assert tables_schema.include_policy.identifier is False
        assert tables_schema.include_policy.database is True
        assert tables_schema.quote_policy.schema is True
        assert tables_schema.quote_policy.identifier is False
        assert tables_schema.quote_policy.database is False

        schemata_schema = info_schema.replace(information_schema_view="SCHEMATA")
        assert schemata_schema.information_schema_view == "SCHEMATA"
        assert schemata_schema.include_policy.schema is False
        assert schemata_schema.include_policy.identifier is True
        assert schemata_schema.include_policy.database is True
        assert schemata_schema.quote_policy.schema is True
        assert schemata_schema.quote_policy.identifier is False
        assert schemata_schema.quote_policy.database is False

        other_schema = info_schema.replace(information_schema_view="SOMETHING_ELSE")
        assert other_schema.information_schema_view == "SOMETHING_ELSE"
        assert other_schema.include_policy.schema is True
        assert other_schema.include_policy.identifier is True
        assert other_schema.include_policy.database is True
        assert other_schema.quote_policy.schema is True
        assert other_schema.quote_policy.identifier is False
        assert other_schema.quote_policy.database is False


class TestBigQueryAdapter(BaseTestBigQueryAdapter):
    def test_copy_table_materialization_table(self):
        adapter = self.get_adapter("oauth")
        adapter.connections = MagicMock()
        adapter.copy_table("source", "destination", "table")
        adapter.connections.copy_bq_table.assert_called_once_with(
            "source", "destination", dbt.adapters.bigquery.impl.WRITE_TRUNCATE
        )

    def test_copy_table_materialization_incremental(self):
        adapter = self.get_adapter("oauth")
        adapter.connections = MagicMock()
        adapter.copy_table("source", "destination", "incremental")
        adapter.connections.copy_bq_table.assert_called_once_with(
            "source", "destination", dbt.adapters.bigquery.impl.WRITE_APPEND
        )

    def test_parse_partition_by(self):
        adapter = self.get_adapter("oauth")

        with self.assertRaises(dbt_common.exceptions.base.DbtValidationError):
            adapter.parse_partition_by("date(ts)")

        with self.assertRaises(dbt_common.exceptions.base.DbtValidationError):
            adapter.parse_partition_by("ts")

        self.assertEqual(
            adapter.parse_partition_by(
                {
                    "field": "ts",
                }
            ).to_dict(omit_none=True),
            {
                "field": "ts",
                "data_type": "date",
                "granularity": "day",
                "time_ingestion_partitioning": False,
                "copy_partitions": False,
            },
        )

        self.assertEqual(
            adapter.parse_partition_by(
                {
                    "field": "ts",
                    "data_type": "date",
                }
            ).to_dict(omit_none=True),
            {
                "field": "ts",
                "data_type": "date",
                "granularity": "day",
                "time_ingestion_partitioning": False,
                "copy_partitions": False,
            },
        )

        self.assertEqual(
            adapter.parse_partition_by(
                {"field": "ts", "data_type": "date", "granularity": "MONTH"}
            ).to_dict(omit_none=True),
            {
                "field": "ts",
                "data_type": "date",
                "granularity": "month",
                "time_ingestion_partitioning": False,
                "copy_partitions": False,
            },
        )

        self.assertEqual(
            adapter.parse_partition_by(
                {"field": "ts", "data_type": "date", "granularity": "YEAR"}
            ).to_dict(omit_none=True),
            {
                "field": "ts",
                "data_type": "date",
                "granularity": "year",
                "time_ingestion_partitioning": False,
                "copy_partitions": False,
            },
        )

        self.assertEqual(
            adapter.parse_partition_by(
                {"field": "ts", "data_type": "timestamp", "granularity": "HOUR"}
            ).to_dict(omit_none=True),
            {
                "field": "ts",
                "data_type": "timestamp",
                "granularity": "hour",
                "time_ingestion_partitioning": False,
                "copy_partitions": False,
            },
        )

        self.assertEqual(
            adapter.parse_partition_by(
                {"field": "ts", "data_type": "timestamp", "granularity": "MONTH"}
            ).to_dict(omit_none=True),
            {
                "field": "ts",
                "data_type": "timestamp",
                "granularity": "month",
                "time_ingestion_partitioning": False,
                "copy_partitions": False,
            },
        )

        self.assertEqual(
            adapter.parse_partition_by(
                {"field": "ts", "data_type": "timestamp", "granularity": "YEAR"}
            ).to_dict(omit_none=True),
            {
                "field": "ts",
                "data_type": "timestamp",
                "granularity": "year",
                "time_ingestion_partitioning": False,
                "copy_partitions": False,
            },
        )

        self.assertEqual(
            adapter.parse_partition_by(
                {"field": "ts", "data_type": "datetime", "granularity": "HOUR"}
            ).to_dict(omit_none=True),
            {
                "field": "ts",
                "data_type": "datetime",
                "granularity": "hour",
                "time_ingestion_partitioning": False,
                "copy_partitions": False,
            },
        )

        self.assertEqual(
            adapter.parse_partition_by(
                {"field": "ts", "data_type": "datetime", "granularity": "MONTH"}
            ).to_dict(omit_none=True),
            {
                "field": "ts",
                "data_type": "datetime",
                "granularity": "month",
                "time_ingestion_partitioning": False,
                "copy_partitions": False,
            },
        )

        self.assertEqual(
            adapter.parse_partition_by(
                {"field": "ts", "data_type": "datetime", "granularity": "YEAR"}
            ).to_dict(omit_none=True),
            {
                "field": "ts",
                "data_type": "datetime",
                "granularity": "year",
                "time_ingestion_partitioning": False,
                "copy_partitions": False,
            },
        )

        self.assertEqual(
            adapter.parse_partition_by(
                {"field": "ts", "time_ingestion_partitioning": True, "copy_partitions": True}
            ).to_dict(omit_none=True),
            {
                "field": "ts",
                "data_type": "date",
                "granularity": "day",
                "time_ingestion_partitioning": True,
                "copy_partitions": True,
            },
        )

        # Invalid, should raise an error
        with self.assertRaises(dbt_common.exceptions.base.DbtValidationError):
            adapter.parse_partition_by({})

        # passthrough
        self.assertEqual(
            adapter.parse_partition_by(
                {
                    "field": "id",
                    "data_type": "int64",
                    "range": {"start": 1, "end": 100, "interval": 20},
                }
            ).to_dict(omit_none=True),
            {
                "field": "id",
                "data_type": "int64",
                "granularity": "day",
                "range": {"start": 1, "end": 100, "interval": 20},
                "time_ingestion_partitioning": False,
                "copy_partitions": False,
            },
        )

    def test_hours_to_expiration(self):
        adapter = self.get_adapter("oauth")
        mock_config = create_autospec(RuntimeConfigObject)
        config = {"hours_to_expiration": 4}
        mock_config.get.side_effect = lambda name: config.get(name)

        expected = {
            "expiration_timestamp": "TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 4 hour)",
        }
        actual = adapter.get_table_options(mock_config, node={}, temporary=False)
        self.assertEqual(expected, actual)

    def test_hours_to_expiration_temporary(self):
        adapter = self.get_adapter("oauth")
        mock_config = create_autospec(RuntimeConfigObject)
        config = {"hours_to_expiration": 4}
        mock_config.get.side_effect = lambda name: config.get(name)

        expected = {
            "expiration_timestamp": ("TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL 12 hour)"),
        }
        actual = adapter.get_table_options(mock_config, node={}, temporary=True)
        self.assertEqual(expected, actual)

    def test_table_kms_key_name(self):
        adapter = self.get_adapter("oauth")
        mock_config = create_autospec(RuntimeConfigObject)
        config = {"kms_key_name": "some_key"}
        mock_config.get.side_effect = lambda name: config.get(name)

        expected = {"kms_key_name": "'some_key'"}
        actual = adapter.get_table_options(mock_config, node={}, temporary=False)
        self.assertEqual(expected, actual)

    def test_view_kms_key_name(self):
        adapter = self.get_adapter("oauth")
        mock_config = create_autospec(RuntimeConfigObject)
        config = {"kms_key_name": "some_key"}
        mock_config.get.side_effect = lambda name: config.get(name)

        expected = {}
        actual = adapter.get_view_options(mock_config, node={})
        self.assertEqual(expected, actual)

    def test_get_common_options_labels_merge(self):
        adapter = self.get_adapter("oauth")
        mock_config = create_autospec(RuntimeConfigObject)
        config = {
            "labels": {"existing_label": "value1"},
            "labels_from_meta": True,
            "meta": {"meta_label": "value2"},
        }
        mock_config.get.side_effect = lambda name: config.get(name)

        expected = {"labels": [("meta_label", "value2"), ("existing_label", "value1")]}
        actual = adapter.get_common_options(mock_config, node={}, temporary=False)
        self.assertEqual(expected, actual)

    def test_get_common_options_labels_no_meta(self):
        adapter = self.get_adapter("oauth")
        mock_config = create_autospec(RuntimeConfigObject)
        config = {
            "labels": {"existing_label": "value1"},
            "labels_from_meta": True,
            "meta": {},
        }
        mock_config.get.side_effect = lambda name: config.get(name)

        expected = {"labels": [("existing_label", "value1")]}
        actual = adapter.get_common_options(mock_config, node={}, temporary=False)
        self.assertEqual(expected, actual)

    def test_get_common_options_labels_no_labels_from_meta(self):
        adapter = self.get_adapter("oauth")
        mock_config = create_autospec(RuntimeConfigObject)
        config = {
            "labels": {"existing_label": "value1"},
            "labels_from_meta": False,
            "meta": {"meta_label": "value2"},
        }
        mock_config.get.side_effect = lambda name: config.get(name)

        expected = {"labels": [("existing_label", "value1")]}
        actual = adapter.get_common_options(mock_config, node={}, temporary=False)
        self.assertEqual(expected, actual)

    def test_get_common_options_no_labels(self):
        adapter = self.get_adapter("oauth")
        mock_config = create_autospec(RuntimeConfigObject)
        config = {
            "labels_from_meta": True,
            "meta": {"meta_label": "value2"},
        }
        mock_config.get.side_effect = lambda name: config.get(name)

        expected = {"labels": [("meta_label", "value2")]}
        actual = adapter.get_common_options(mock_config, node={}, temporary=False)
        self.assertEqual(expected, actual)

    def test_get_common_options_empty(self):
        adapter = self.get_adapter("oauth")
        mock_config = create_autospec(RuntimeConfigObject)
        config = {}
        mock_config.get.side_effect = lambda name: config.get(name)

        expected = {}
        actual = adapter.get_common_options(mock_config, node={}, temporary=False)
        self.assertEqual(expected, actual)


class TestBigQueryFilterCatalog(unittest.TestCase):
    def test__catalog_filter_table(self):
        used_schemas = [["a", "B"], ["a", "1234"]]
        column_names = ["table_name", "table_database", "table_schema", "something"]
        rows = [
            ["foo", "a", "b", "1234"],  # include
            ["foo", "a", "1234", "1234"],  # include, w/ table schema as str
            ["foo", "c", "B", "1234"],  # skip
            ["1234", "A", "B", "1234"],  # include, w/ table name as str
        ]
        table = agate.Table(rows, column_names, agate_helper.DEFAULT_TYPE_TESTER)

        result = BigQueryAdapter._catalog_filter_table(table, used_schemas)
        assert len(result) == 3
        for row in result.rows:
            assert isinstance(row["table_schema"], str)
            assert isinstance(row["table_database"], str)
            assert isinstance(row["table_name"], str)
            assert isinstance(row["something"], decimal.Decimal)


class TestBigQueryAdapterConversions(TestAdapterConversions):
    def test_convert_text_type(self):
        rows = [
            ["", "a1", "stringval1"],
            ["", "a2", "stringvalasdfasdfasdfa"],
            ["", "a3", "stringval3"],
        ]
        agate_table = self._make_table_of(rows, agate.Text)
        expected = ["string", "string", "string"]
        for col_idx, expect in enumerate(expected):
            assert BigQueryAdapter.convert_text_type(agate_table, col_idx) == expect

    def test_convert_number_type(self):
        rows = [
            ["", "23.98", "-1"],
            ["", "12.78", "-2"],
            ["", "79.41", "-3"],
        ]
        agate_table = self._make_table_of(rows, agate.Number)
        expected = ["int64", "float64", "int64"]
        for col_idx, expect in enumerate(expected):
            assert BigQueryAdapter.convert_number_type(agate_table, col_idx) == expect

    def test_convert_boolean_type(self):
        rows = [
            ["", "false", "true"],
            ["", "false", "false"],
            ["", "false", "true"],
        ]
        agate_table = self._make_table_of(rows, agate.Boolean)
        expected = ["bool", "bool", "bool"]
        for col_idx, expect in enumerate(expected):
            assert BigQueryAdapter.convert_boolean_type(agate_table, col_idx) == expect

    def test_convert_datetime_type(self):
        rows = [
            ["", "20190101T01:01:01Z", "2019-01-01 01:01:01"],
            ["", "20190102T01:01:01Z", "2019-01-01 01:01:01"],
            ["", "20190103T01:01:01Z", "2019-01-01 01:01:01"],
        ]
        agate_table = self._make_table_of(
            rows, [agate.DateTime, agate_helper.ISODateTime, agate.DateTime]
        )
        expected = ["datetime", "datetime", "datetime"]
        for col_idx, expect in enumerate(expected):
            assert BigQueryAdapter.convert_datetime_type(agate_table, col_idx) == expect

    def test_convert_date_type(self):
        rows = [
            ["", "2019-01-01", "2019-01-04"],
            ["", "2019-01-02", "2019-01-04"],
            ["", "2019-01-03", "2019-01-04"],
        ]
        agate_table = self._make_table_of(rows, agate.Date)
        expected = ["date", "date", "date"]
        for col_idx, expect in enumerate(expected):
            assert BigQueryAdapter.convert_date_type(agate_table, col_idx) == expect

    def test_convert_time_type(self):
        # dbt's default type testers actually don't have a TimeDelta at all.
        agate.TimeDelta
        rows = [
            ["", "120s", "10s"],
            ["", "3m", "11s"],
            ["", "1h", "12s"],
        ]
        agate_table = self._make_table_of(rows, agate.TimeDelta)
        expected = ["time", "time", "time"]
        for col_idx, expect in enumerate(expected):
            assert BigQueryAdapter.convert_time_type(agate_table, col_idx) == expect

    # The casing in this case can't be enforced on the API side,
    # so we have to validate that we have a case-insensitive comparison
    def test_partitions_match(self):
        table = Table.from_api_repr(
            {
                "tableReference": {
                    "projectId": "test-project",
                    "datasetId": "test_dataset",
                    "tableId": "test_table",
                },
                "timePartitioning": {"type": "DAY", "field": "ts"},
            }
        )
        partition_config = PartitionConfig.parse(
            {
                "field": "TS",
                "data_type": "date",
                "granularity": "day",
                "time_ingestion_partitioning": False,
                "copy_partitions": False,
            }
        )
        assert BigQueryAdapter._partitions_match(table, partition_config) is True


class TestBigQueryGrantAccessTo(BaseTestBigQueryAdapter):
    entity = BigQueryRelation.from_dict(
        {
            "type": None,
            "path": {"database": "test-project", "schema": "test_schema", "identifier": "my_view"},
            "quote_policy": {"identifier": False},
        }
    )

    def setUp(self):
        super().setUp()
        self.mock_dataset: MagicMock = MagicMock(name="GrantMockDataset")
        self.mock_dataset.access_entries = [AccessEntry(None, "table", self.entity)]
        self.mock_client: MagicMock = MagicMock(name="MockBQClient")
        self.mock_client.get_dataset.return_value = self.mock_dataset
        self.mock_connection = MagicMock(name="MockConn")
        self.mock_connection.handle = self.mock_client
        self.mock_connection_mgr = MagicMock(
            name="GrantAccessMockMgr",
        )
        self.mock_connection_mgr.get_thread_connection.return_value = self.mock_connection
        _adapter = self.get_adapter("oauth")
        _adapter.connections = self.mock_connection_mgr
        self.adapter = _adapter

    def test_grant_access_to_calls_update_with_valid_access_entry(self):
        a_different_entity = BigQueryRelation.from_dict(
            {
                "type": None,
                "path": {
                    "database": "another-test-project",
                    "schema": "test_schema_2",
                    "identifier": "my_view",
                },
                "quote_policy": {"identifier": True},
            }
        )
        grant_target_dict = {"dataset": "someOtherDataset", "project": "someProject"}
        self.adapter.grant_access_to(
            entity=a_different_entity,
            entity_type="view",
            role=None,
            grant_target_dict=grant_target_dict,
        )
        self.mock_client.update_dataset.assert_called_once()


@pytest.mark.parametrize(
    ["input", "output"],
    [
        ("ABC", "abc"),
        ("a c", "a_c"),
        ("a ", "a"),
    ],
)
def test_sanitize_label(input, output):
    assert _sanitize_label(input) == output


@pytest.mark.parametrize(
    "label_length",
    [64, 65, 100],
)
def test_sanitize_label_length(label_length):
    random_string = "".join(
        random.choice(string.ascii_uppercase + string.digits) for i in range(label_length)
    )
    assert len(_sanitize_label(random_string)) <= _VALIDATE_LABEL_LENGTH_LIMIT
