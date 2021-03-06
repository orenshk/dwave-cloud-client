"""Try to load solver data from mock servers."""
from __future__ import division, absolute_import, print_function, unicode_literals

import os
import json
import unittest
import requests_mock

try:
    import unittest.mock as mock
except ImportError:
    import mock

from dwave.cloud.qpu import Client, Solver
from dwave.cloud.exceptions import InvalidAPIResponseError
from dwave.cloud.config import legacy_load_config

from .test_config import iterable_mock_open, configparser_open_namespace


url = 'https://dwavesys.com'
token = 'abc123abc123abc123abc123abc123abc123'
solver_name = 'test_solver'
second_solver_name = 'test_solver2'

bad_url = 'https://not-a-subdomain.dwavesys.com'
bad_token = '------------------------'


def solver_object(id_, incomplete=False):
    """Return string describing a single solver."""
    obj = {
        "properties": {
            "supported_problem_types": ["qubo", "ising"],
            "qubits": [0, 1, 2],
            "couplers": [[0, 1], [0, 2], [1, 2]],
            "num_qubits": 3,
            "parameters": {"num_reads": "Number of samples to return."}
        },
        "id": id_,
        "description": "A test solver"
    }

    if incomplete:
        del obj['properties']['parameters']

    return json.dumps(obj)


# Define the endpoinds
all_solver_url = '{}/solvers/remote/'.format(url)
solver1_url = '{}/solvers/remote/{}/'.format(url, solver_name)
solver2_url = '{}/solvers/remote/{}/'.format(url, second_solver_name)


def setup_server(m):
    """Add endpoints to the server."""
    # Content strings
    first_solver_response = solver_object(solver_name)
    second_solver_response = solver_object(second_solver_name)
    two_solver_response = '[' + first_solver_response + ',' + second_solver_response + ']'

    # Setup the server
    headers = {'X-Auth-Token': token}
    m.get(requests_mock.ANY, status_code=404)
    m.get(all_solver_url, status_code=403, request_headers={})
    m.get(solver1_url, status_code=403, request_headers={})
    m.get(solver2_url, status_code=403, request_headers={})
    m.get(all_solver_url, text=two_solver_response, request_headers=headers)
    m.get(solver1_url, text=first_solver_response, request_headers=headers)
    m.get(solver2_url, text=second_solver_response, request_headers=headers)


class MockConnectivityTests(unittest.TestCase):
    """Test connecting some related failure modes."""

    def test_bad_url(self):
        """Connect with a bad URL."""
        with requests_mock.mock() as m:
            setup_server(m)
            with self.assertRaises(IOError):
                client = Client(bad_url, token)
                client.get_solvers()

    def test_bad_token(self):
        """Connect with a bad token."""
        with requests_mock.mock() as m:
            setup_server(m)
            with self.assertRaises(IOError):
                client = Client(url, bad_token)
                client.get_solvers()

    def test_good_connection(self):
        """Connect with a valid URL and token."""
        with requests_mock.mock() as m:
            setup_server(m)
            client = Client(url, token)
            self.assertTrue(len(client.get_solvers()) > 0)


class MockSolverLoading(unittest.TestCase):
    """Test loading solvers in a few different configurations.

    Note:
        A mock server does not test authentication.

    Expect three responses from the server for /solvers/*:
        - A single solver when a single solver is requested
        - A list of single solver objects when all solvers are requested
        - A 404 error when the requested solver does not exist
    An additional condition to test for is that the server may have configured
    a given solver such that it does not provide all the required information
    about it.
    """

    def test_load_solver(self):
        """Load a single solver."""
        with requests_mock.mock() as m:
            setup_server(m)

            # test default, cached solver get
            client = Client(url, token)
            solver = client.get_solver(solver_name)
            self.assertEqual(solver.id, solver_name)

            # fetch solver not present in cache
            client._solvers = {}
            self.assertEqual(client.get_solver(solver_name).id, solver_name)

            # re-fetch solver present in cache
            solver = client.get_solver(solver_name)
            solver.id = 'different-solver'
            self.assertEqual(client.get_solver(solver_name, refresh=True).id, solver_name)

    def test_load_all_solvers(self):
        """Load the list of solver names."""
        with requests_mock.mock() as m:
            setup_server(m)

            # test default case, fetch all solvers for the first time
            client = Client(url, token)
            self.assertEqual(len(client.get_solvers()), 2)

            # test default refresh
            client._solvers = {}
            self.assertEqual(len(client.get_solvers()), 0)

            # test no refresh
            client._solvers = {}
            self.assertEqual(len(client.get_solvers(refresh=False)), 0)

            # test refresh
            client._solvers = {}
            self.assertEqual(len(client.get_solvers(refresh=True)), 2)

    def test_load_missing_solver(self):
        """Try to load a solver that does not exist."""
        with requests_mock.mock() as m:
            m.get(requests_mock.ANY, status_code=404)
            client = Client(url, token)
            with self.assertRaises(KeyError):
                client.get_solver(solver_name)

    def test_load_solver_missing_data(self):
        """Try to load a solver that has incomplete data."""
        with requests_mock.mock() as m:
            m.get(solver1_url, text=solver_object(solver_name, True))
            client = Client(url, token)
            with self.assertRaises(InvalidAPIResponseError):
                client.get_solver(solver_name)

    def test_load_solver_broken_response(self):
        """Try to load a solver for which the server has returned a truncated response."""
        with requests_mock.mock() as m:
            body = solver_object(solver_name)
            m.get(solver1_url, text=body[0:len(body)//2])
            client = Client(url, token)
            with self.assertRaises(ValueError):
                client.get_solver(solver_name)

    def test_solver_filtering_in_client(self):
        self.assertTrue(Client.is_solver_handled(Solver(None, json.loads(solver_object('test')))))
        self.assertFalse(Client.is_solver_handled(Solver(None, json.loads(solver_object('c4-sw_')))))
        self.assertFalse(None)


class GetEvent(Exception):
    """Throws exception when mocked client submits an HTTP GET request."""

    def __init__(self, url):
        """Return the URL of the request with the exception for test verification."""
        self.url = url

    @staticmethod
    def handle(path, *args, **kwargs):
        """Callback function that can be inserted into a mock."""
        raise GetEvent(path)


config_body = """
prod|file-prod-url,file-prod-token
alpha|file-alpha-url,file-alpha-token,,alpha-solver
"""


# patch the new config loading mechanism, to test only legacy config loading
@mock.patch("dwave.cloud.config.detect_configfile_path", lambda: None)
class MockConfiguration(unittest.TestCase):
    """Ensure that the precedence of configuration sources is followed."""

    def setUp(self):
        # clear `config_load`-relevant environment variables before testing, so
        # we only need to patch the ones that we are currently testing
        for key in frozenset(os.environ.keys()):
            if key.startswith("DWAVE_") or key.startswith("DW_INTERNAL__"):
                os.environ.pop(key, None)

    def test_explicit_only(self):
        """Specify information only through function arguments."""
        client = Client.from_config(config_file='nonexisting',
                                    endpoint='arg-url', token='arg-token')
        client.session.get = GetEvent.handle
        try:
            client.get_solver('arg-solver')
        except GetEvent as event:
            self.assertTrue(event.url.startswith('arg-url'))
            return
        self.fail()

    def test_nothing(self):
        """With no values set, we should get an error when trying to create Client."""
        with self.assertRaises(ValueError):
            Client.from_config(config_file='nonexisting')

    def test_explicit_with_file(self):
        """With arguments and a config file, the config file should be ignored."""
        with mock.patch("dwave.cloud.config.open", mock.mock_open(read_data=config_body), create=True):
            client = Client.from_config(endpoint='arg-url', token='arg-token')
            client.session.get = GetEvent.handle
            try:
                client.get_solver('arg-solver')
            except GetEvent as event:
                self.assertTrue(event.url.startswith('arg-url'))
                return
            self.fail()

    def test_only_file(self):
        """With no arguments or environment variables, the default connection from the config file should be used."""
        with mock.patch("dwave.cloud.config.open", mock.mock_open(read_data=config_body), create=True):
            client = Client.from_config()
            client.session.get = GetEvent.handle
            try:
                client.get_solver('arg-solver')
            except GetEvent as event:
                self.assertTrue(event.url.startswith('file-prod-url'))
                return
            self.fail()

    def test_only_file_key(self):
        """If give a name from the config file the proper URL should be loaded."""
        with mock.patch("dwave.cloud.config.open", mock.mock_open(read_data=config_body), create=True):
            with mock.patch(configparser_open_namespace, iterable_mock_open(config_body), create=True):
                # this will try parsing legacy format as new, fail,
                # then try parsing it as legacy config
                client = Client.from_config(profile='alpha')
                client.session.get = GetEvent.handle
                try:
                    client.get_solver('arg-solver')
                except GetEvent as event:
                    self.assertTrue(event.url.startswith('file-alpha-url'))
                    return
                self.fail()

    def test_env_with_file_set(self):
        """With environment variables and a config file, the config file should be ignored."""
        with mock.patch("dwave.cloud.config.open", mock.mock_open(read_data=config_body), create=True):
            with mock.patch.dict(os.environ, {'DW_INTERNAL__HTTPLINK': 'env-url', 'DW_INTERNAL__TOKEN': 'env-token'}):
                client = Client.from_config()
                client.session.get = GetEvent.handle
                try:
                    client.get_solver('arg-solver')
                except GetEvent as event:
                    self.assertTrue(event.url.startswith('env-url'))
                    return
                self.fail()

    def test_env_args_set(self):
        """With arguments and environment variables, the environment variables should be ignored."""
        with mock.patch.dict(os.environ, {'DW_INTERNAL__HTTPLINK': 'env-url', 'DW_INTERNAL__TOKEN': 'env-token'}):
            client = Client.from_config(endpoint='args-url', token='args-token')
            client.session.get = GetEvent.handle
            try:
                client.get_solver('arg-solver')
            except GetEvent as event:
                self.assertTrue(event.url.startswith('args-url'))
                return
            self.fail()

    def test_file_read_error(self):
        """On config file read error, we should fail with `IOError`."""
        with mock.patch("dwave.cloud.config.open", side_effect=OSError, create=True):
            self.assertRaises(IOError, legacy_load_config)

    def test_file_format_error(self):
        """Config parsing error should be suppressed."""
        with mock.patch("dwave.cloud.config.open", mock.mock_open(read_data="|\na|b,c"), create=True):
            self.assertEqual(legacy_load_config(key='a'), ('b', 'c', None, None))
        with mock.patch("dwave.cloud.config.open", mock.mock_open(read_data="|"), create=True):
            self.assertRaises(ValueError, legacy_load_config)
