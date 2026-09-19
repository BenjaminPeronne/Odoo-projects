import unittest
from unittest import mock

from odoo_manager_core.config import ManagerSettings
from odoo_manager_core.docker_api import (
    DockerEngineClient,
    EngineEndpoint,
    EngineUnavailable,
    engine_endpoint,
    parse_docker_host,
    parse_http_response,
)


def response(body, *, status=200, chunked=False):
    if chunked:
        payload = b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body)
        headers = b"Transfer-Encoding: chunked"
    else:
        payload = body
        headers = b"Content-Length: %d" % len(body)
    return b"HTTP/1.1 %d OK\r\nContent-Type: application/json\r\n%s\r\n\r\n%s" % (status, headers, payload)


class HttpParsingTests(unittest.TestCase):
    def test_reads_a_plain_response(self):
        self.assertEqual((200, b'{"Version":"29.7.2"}'), parse_http_response(response(b'{"Version":"29.7.2"}')))

    def test_reads_a_chunked_response(self):
        status, body = parse_http_response(response(b'[{"Names":["/traefik"]}]', chunked=True))
        self.assertEqual((200, b'[{"Names":["/traefik"]}]'), (status, body))

    def test_rejects_a_truncated_response(self):
        with self.assertRaises(ValueError):
            parse_http_response(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n")


class EndpointSelectionTests(unittest.TestCase):
    """L'API ne doit servir que lorsque la CLI parlerait au même moteur."""

    def setUp(self):
        self.settings = ManagerSettings.from_dict({}, "/tmp/workspace")

    def endpoint(self, **kwargs):
        kwargs.setdefault("environ", {})
        kwargs.setdefault("platform_name", "windows")
        kwargs.setdefault("home", "/home/test")
        kwargs.setdefault("exists", lambda path: True)
        return engine_endpoint(self.settings, **kwargs)

    def test_windows_named_pipe_by_default(self):
        with mock.patch("odoo_manager_core.docker_api.current_docker_context", return_value="default"):
            self.assertEqual(EngineEndpoint("npipe", r"\\.\pipe\docker_engine"), self.endpoint())

    def test_unix_socket_on_linux(self):
        with mock.patch("odoo_manager_core.docker_api.current_docker_context", return_value=""):
            self.assertEqual(EngineEndpoint("unix", "/var/run/docker.sock"), self.endpoint(platform_name="linux"))

    def test_skipped_when_docker_runs_in_wsl(self):
        self.assertIsNone(self.endpoint(uses_wsl=True))

    def test_skipped_for_a_remote_docker_host(self):
        self.assertIsNone(self.endpoint(environ={"DOCKER_HOST": "tcp://build-server:2376"}))

    def test_skipped_for_a_custom_context(self):
        with mock.patch("odoo_manager_core.docker_api.current_docker_context", return_value="remote-prod"):
            self.assertIsNone(self.endpoint())

    def test_skipped_for_a_custom_docker_executable(self):
        self.settings = ManagerSettings.from_dict({"docker_executable": "/opt/podman/docker"}, "/tmp/workspace")
        self.assertIsNone(self.endpoint())

    def test_follows_an_explicit_local_docker_host(self):
        self.assertEqual(
            EngineEndpoint("unix", "/run/user/1000/docker.sock"),
            self.endpoint(environ={"DOCKER_HOST": "unix:///run/user/1000/docker.sock"}),
        )

    def test_parses_a_named_pipe_docker_host(self):
        self.assertEqual(
            EngineEndpoint("npipe", r"\\.\pipe\docker_engine"),
            parse_docker_host("npipe:////./pipe/docker_engine"),
        )


class EngineClientTests(unittest.TestCase):
    def client(self, reader):
        return DockerEngineClient(EngineEndpoint("unix", "/var/run/docker.sock"), reader=reader)

    def test_reads_the_server_version(self):
        client = self.client(lambda _endpoint, _request: response(b'{"Version":"29.7.2","ApiVersion":"1.52"}'))
        self.assertEqual("29.7.2", client.server_version())

    def test_maps_container_names_to_states(self):
        body = b'[{"Names":["/traefik"],"State":"running"},{"Names":["/odoo-DEMO"],"State":"exited"}]'
        client = self.client(lambda _endpoint, _request: response(body, chunked=True))
        self.assertEqual({"traefik": "running", "odoo-DEMO": "exited"}, client.container_states())

    def test_a_stopped_engine_hands_back_to_the_cli(self):
        def refuse(_endpoint, _request):
            raise FileNotFoundError("le moteur Docker est arrêté")

        with self.assertRaises(EngineUnavailable):
            self.client(refuse).server_version()

    def test_an_http_error_hands_back_to_the_cli(self):
        client = self.client(lambda _endpoint, _request: response(b'{"message":"nope"}', status=500))
        with self.assertRaises(EngineUnavailable):
            client.container_states()

    def test_a_hanging_engine_does_not_block_the_request(self):
        import threading

        blocked = threading.Event()
        client = DockerEngineClient(
            EngineEndpoint("unix", "/var/run/docker.sock"),
            reader=lambda _endpoint, _request: blocked.wait(30),
            timeout=0.2,
        )
        try:
            with self.assertRaises(EngineUnavailable):
                client.server_version()
        finally:
            blocked.set()


if __name__ == "__main__":
    unittest.main()
