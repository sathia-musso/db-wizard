"""
Tests for MySQLEngine.
All tests mock subprocess calls - no real MySQL server needed.
"""

import subprocess
from unittest.mock import patch, MagicMock

import pytest

from db_wizard.engines.mysql import MySQLEngine, parse_mysql_uri, _build_mysql_args
from db_wizard.engine import DatabaseEngine, EngineFactory


class TestParseMysqlUri:
    """Test MySQL URI parsing"""

    def test_full_uri(self):
        params = parse_mysql_uri("mysql://user:pass@host.com:3307/mydb")
        assert params['host'] == 'host.com'
        assert params['port'] == 3307
        assert params['user'] == 'user'
        assert params['password'] == 'pass'
        assert params['database'] == 'mydb'

    def test_default_port(self):
        params = parse_mysql_uri("mysql://user:pass@host/db")
        assert params['port'] == 3306

    def test_no_password(self):
        params = parse_mysql_uri("mysql://root@localhost/test")
        assert params['user'] == 'root'
        assert params['password'] == ''

    def test_url_encoded_password(self):
        params = parse_mysql_uri("mysql://user:p%40ss%23word@host/db")
        assert params['password'] == 'p@ss#word'

    def test_no_database(self):
        params = parse_mysql_uri("mysql://user:pass@host")
        assert params['database'] is None or params['database'] == ''

    def test_localhost_defaults(self):
        params = parse_mysql_uri("mysql://root@localhost/testdb")
        assert params['host'] == 'localhost'
        assert params['port'] == 3306
        assert params['user'] == 'root'


class TestBuildMysqlArgs:
    """Test MySQL CLI argument building"""

    def test_with_password(self):
        params = {'host': 'server.com', 'port': 3307, 'user': 'admin', 'password': 'secret'}
        args = _build_mysql_args(params)
        assert '-h' in args
        assert 'server.com' in args
        assert '-P' in args
        assert '3307' in args
        assert '-u' in args
        assert 'admin' in args
        assert '-psecret' not in args

    def test_without_password(self):
        params = {'host': 'localhost', 'port': 3306, 'user': 'root', 'password': ''}
        args = _build_mysql_args(params)
        assert '-psecret' not in args
        # Should not have any -p flag
        assert not any(a.startswith('-p') for a in args)


class TestMySQLEngineABC:
    """Test MySQLEngine implements DatabaseEngine correctly"""

    def test_is_database_engine_subclass(self):
        assert issubclass(MySQLEngine, DatabaseEngine)

    def test_table_term(self):
        engine = MySQLEngine("mysql://root@localhost/test")
        assert engine.table_term == "table"
        assert engine.table_term_plural == "tables"

    def test_scheme(self):
        engine = MySQLEngine("mysql://root@localhost/test")
        assert engine.scheme == "mysql"

    def test_engine_factory_creates_mysql(self):
        engine = EngineFactory.create("mysql://user:pass@host/db")
        assert isinstance(engine, MySQLEngine)

    def test_engine_factory_rejects_unknown(self):
        with pytest.raises(ValueError, match="Unsupported"):
            EngineFactory.create("unknown://localhost/db")


class TestMySQLEngineConnection:
    """Test connection methods"""

    @patch('subprocess.run')
    def test_connect_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="1", stderr="")
        engine = MySQLEngine("mysql://root@localhost/test")
        result = engine.connect()
        assert result is engine  # Returns self

    @patch('subprocess.run')
    def test_connect_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="Access denied")
        engine = MySQLEngine("mysql://root@localhost/test")
        with pytest.raises(ConnectionError, match="Access denied"):
            engine.connect()

    @patch('subprocess.run')
    def test_test_connection_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="5\n", stderr="")
        engine = MySQLEngine("mysql://root@localhost/test")
        success, msg = engine.test_connection()
        assert success is True
        assert "5" in msg

    @patch('subprocess.run')
    def test_test_connection_failure(self, mock_run):
        mock_run.side_effect = Exception("Connection refused")
        engine = MySQLEngine("mysql://root@localhost/test")
        success, msg = engine.test_connection()
        assert success is False

    def test_close_does_nothing(self):
        """Close should not raise (CLI-based, no persistent connection)"""
        engine = MySQLEngine("mysql://root@localhost/test")
        engine.close()  # Should not raise


class TestMySQLEngineIntrospection:
    """Test list_databases, list_tables, count_rows"""

    @patch('subprocess.run')
    def test_list_databases(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="myapp\t5\t10.5\nother_db\t3\t2.1\nmysql\t30\t5.0\n"
        )
        engine = MySQLEngine("mysql://root@localhost")
        dbs = engine.list_databases()

        # Should exclude 'mysql' system db
        db_names = [d['name'] for d in dbs]
        assert 'myapp' in db_names
        assert 'other_db' in db_names
        assert 'mysql' not in db_names

    @patch('subprocess.run')
    def test_list_tables(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="users\t1000\t3\nposts\t5000\t2\n"
        )
        engine = MySQLEngine("mysql://root@localhost/myapp")
        tables = engine.list_tables("myapp")

        assert len(tables) == 2
        assert tables[0]['name'] == 'users'
        assert tables[0]['rows'] == 1000
        assert tables[0]['indexes'] == 3

    @patch('subprocess.run')
    def test_count_rows(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="42\n")
        engine = MySQLEngine("mysql://root@localhost/myapp")
        count = engine.count_rows("myapp", "users")
        assert count == 42


class TestMySQLEngineTools:
    """Test check_tools"""

    @patch('shutil.which')
    def test_check_tools_both_available(self, mock_which):
        mock_which.return_value = '/usr/bin/mysql'
        engine = MySQLEngine("mysql://root@localhost")
        tools = engine.check_tools()
        assert tools['mysqldump'] is True
        assert tools['mysql'] is True

    @patch('shutil.which')
    def test_check_tools_none_available(self, mock_which):
        mock_which.return_value = None
        engine = MySQLEngine("mysql://root@localhost")
        tools = engine.check_tools()
        assert tools['mysqldump'] is False
        assert tools['mysql'] is False


class TestMySQLEngineCopy:
    """Test copy operations (mysqldump | mysql pipe)"""

    @patch('shutil.which', return_value='/usr/bin/mysql')
    @patch('subprocess.Popen')
    @patch('subprocess.run')
    def test_copy_table_success(self, mock_run, mock_popen, mock_which):
        """Test successful single table copy via pipe"""
        # Mock row count queries
        mock_run.return_value = MagicMock(returncode=0, stdout="100\n")

        # Mock pipe processes
        mock_dump = MagicMock()
        mock_dump.stdout = MagicMock()
        mock_dump.stderr = MagicMock()
        mock_dump.stderr.read.return_value = b""

        mock_restore = MagicMock()
        mock_restore.returncode = 0
        mock_restore.communicate.return_value = (None, b"")

        mock_popen.side_effect = [mock_dump, mock_restore]

        source = MySQLEngine("mysql://user:pass@remote/srcdb")
        target = MySQLEngine("mysql://user:pass@localhost/tgtdb")

        result = target.copy(
            source_engine=source,
            source_db="srcdb", source_table="users",
            target_db="tgtdb", target_table="users"
        )

        assert result['method'] == 'mysqldump'
        assert result['documents_copied'] >= 0

    @patch('shutil.which', return_value='/usr/bin/mysql')
    @patch('subprocess.Popen')
    @patch('subprocess.run')
    def test_copy_table_interrupted(self, mock_run, mock_popen, mock_which):
        """Ctrl-C during the pipe must kill both processes and report an interruption"""
        mock_run.return_value = MagicMock(returncode=0, stdout="100\n")

        mock_dump = MagicMock()
        mock_dump.stdout = MagicMock()
        mock_dump.stderr = MagicMock()

        mock_restore = MagicMock()
        mock_restore.communicate.side_effect = KeyboardInterrupt()

        mock_popen.side_effect = [mock_dump, mock_restore]

        source = MySQLEngine("mysql://user:pass@remote/srcdb")
        target = MySQLEngine("mysql://user:pass@localhost/tgtdb")

        result = target.copy(
            source_engine=source,
            source_db="srcdb", source_table="users",
            target_db="tgtdb", target_table="users"
        )

        assert result['method'] == 'mysqldump_interrupted'
        mock_restore.kill.assert_called_once()
        mock_dump.kill.assert_called_once()

    @patch('shutil.which', return_value='/usr/bin/mysql')
    @patch('subprocess.Popen')
    @patch('subprocess.run')
    def test_copy_table_has_no_wall_clock_timeout(self, mock_run, mock_popen, mock_which):
        """
        A slow dump must never be killed on a deadline: communicate() is called
        without a timeout so a multi-hour copy of a large table runs to the end.
        """
        mock_run.return_value = MagicMock(returncode=0, stdout="100\n")

        mock_dump = MagicMock()
        mock_dump.stdout = MagicMock()
        mock_dump.stderr = MagicMock()
        mock_dump.stderr.read.return_value = b""

        mock_restore = MagicMock()
        mock_restore.returncode = 0
        mock_restore.communicate.return_value = (None, b"")

        mock_popen.side_effect = [mock_dump, mock_restore]

        source = MySQLEngine("mysql://user:pass@remote/srcdb")
        target = MySQLEngine("mysql://user:pass@localhost/tgtdb")

        target.copy(
            source_engine=source,
            source_db="srcdb", source_table="users",
            target_db="tgtdb", target_table="users"
        )

        assert 'timeout' not in mock_restore.communicate.call_args.kwargs
        assert mock_restore.communicate.call_args.args == ()
        mock_restore.kill.assert_not_called()
        mock_dump.kill.assert_not_called()

    @patch('shutil.which', return_value=None)
    def test_copy_without_tools(self, mock_which):
        """Copy should fail gracefully when tools missing"""
        source = MySQLEngine("mysql://user:pass@remote/srcdb")
        target = MySQLEngine("mysql://user:pass@localhost/tgtdb")

        result = target.copy(
            source_engine=source,
            source_db="srcdb", source_table="users",
            target_db="tgtdb", target_table="users"
        )

        assert result['method'] == 'failed'
        assert result['documents_copied'] == 0


class TestMySQLEngineBuildUri:
    """Test URI building"""

    def test_simple(self):
        uri = MySQLEngine.build_connection_uri("localhost", database="mydb")
        assert uri == "mysql://root@localhost:3306/mydb"

    def test_with_password(self):
        uri = MySQLEngine.build_connection_uri(
            "server.com", port=3307,
            username="admin", password="s3cret",
            database="prod"
        )
        assert "admin" in uri
        assert "s3cret" in uri
        assert "server.com:3307" in uri
        assert "/prod" in uri

    def test_special_chars_in_password(self):
        uri = MySQLEngine.build_connection_uri(
            "localhost", username="user", password="p@ss#word"
        )
        # Password should be URL-encoded
        assert "p%40ss%23word" in uri
