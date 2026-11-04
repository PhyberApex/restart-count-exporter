#!/usr/bin/env python3
import http.server
import json
import os
import socket
import socketserver
import tempfile
import threading
import unittest
from unittest.mock import patch

import exporter


def inspect_payload(name, restart_count, labels=None):
    return {"Name": f"/{name}", "RestartCount": restart_count, "Config": {"Labels": labels}}


def compose_labels(project, service):
    return {exporter.COMPOSE_PROJECT_LABEL: project, exporter.COMPOSE_SERVICE_LABEL: service}


class Collect(unittest.TestCase):
    def test_single_container(self):
        with patch("exporter._get_json", side_effect=[[{"Id": "abc123"}], inspect_payload("web", 7)]):
            samples = exporter.collect()

        self.assertEqual(samples, ['docker_container_restart_count{name="web",compose_project="",compose_service=""} 7'])

    def test_multiple_containers(self):
        inspects = [inspect_payload("foo", 0), inspect_payload("bar", 3)]
        with patch("exporter._get_json", side_effect=[[{"Id": "a"}, {"Id": "b"}], *inspects]):
            samples = exporter.collect()

        self.assertEqual(samples, [
            'docker_container_restart_count{name="foo",compose_project="",compose_service=""} 0',
            'docker_container_restart_count{name="bar",compose_project="",compose_service=""} 3',
        ])

    def test_no_containers_returns_empty_list(self):
        with patch("exporter._get_json", return_value=[]):
            self.assertEqual(exporter.collect(), [])

    def test_propagates_api_errors(self):
        with patch("exporter._get_json", side_effect=RuntimeError("proxy unreachable")):
            with self.assertRaises(RuntimeError):
                exporter.collect()

    def test_container_name_is_escaped_in_labels(self):
        payload = inspect_payload('Rogue "container" \\ 1', 1)
        with patch("exporter._get_json", side_effect=[[{"Id": "a"}], payload]):
            samples = exporter.collect()

        self.assertIn('name="Rogue \\"container\\" \\\\ 1"', samples[0])

    def test_leading_slash_stripped_from_name(self):
        with patch("exporter._get_json", side_effect=[[{"Id": "a"}], inspect_payload("web", 2)]):
            samples = exporter.collect()

        self.assertIn('name="web"', samples[0])
        self.assertNotIn('name="/web"', samples[0])

    def test_compose_labels_are_exported(self):
        payload = inspect_payload("stack-web-1", 2, compose_labels("stack", "web"))
        with patch("exporter._get_json", side_effect=[[{"Id": "a"}], payload]):
            samples = exporter.collect()

        self.assertEqual(samples, ['docker_container_restart_count{name="stack-web-1",compose_project="stack",compose_service="web"} 2'])

    def test_compose_labels_are_escaped(self):
        payload = inspect_payload("x", 0, compose_labels('pro"ject', "svc\nname"))
        with patch("exporter._get_json", side_effect=[[{"Id": "a"}], payload]):
            samples = exporter.collect()

        self.assertIn('compose_project="pro\\"ject",compose_service="svc\\nname"', samples[0])

    def test_container_without_a_config_block_still_renders(self):
        payload = {"Name": "/bare", "RestartCount": 1}
        with patch("exporter._get_json", side_effect=[[{"Id": "a"}], payload]):
            samples = exporter.collect()

        self.assertEqual(samples, ['docker_container_restart_count{name="bare",compose_project="",compose_service=""} 1'])


class Render(unittest.TestCase):
    def test_metric_has_help_and_type(self):
        body = exporter.render([], 1)
        self.assertIn(f"# HELP {exporter.RESTART_COUNT_METRIC}", body)
        self.assertIn(f"# TYPE {exporter.RESTART_COUNT_METRIC} counter", body)
        self.assertIn(f"# HELP {exporter.COLLECTOR_UP_METRIC}", body)
        self.assertIn(f"# TYPE {exporter.COLLECTOR_UP_METRIC} gauge", body)

    def test_collector_up_value_is_rendered(self):
        self.assertIn(f"{exporter.COLLECTOR_UP_METRIC} 1", exporter.render([], 1))
        self.assertIn(f"{exporter.COLLECTOR_UP_METRIC} 0", exporter.render([], 0))

    def test_error_comment_is_included_when_given(self):
        self.assertIn("# error: boom", exporter.render([], 0, error=RuntimeError("boom")))

    def test_no_error_comment_when_none_given(self):
        self.assertNotIn("# error:", exporter.render([], 1))

    def test_samples_are_included(self):
        body = exporter.render(['docker_container_restart_count{name="x"} 5'], 1)
        self.assertIn('docker_container_restart_count{name="x"} 5', body)


class Scrape(unittest.TestCase):
    def test_healthy_scrape_reports_collector_up_and_samples(self):
        with patch("exporter._get_json", side_effect=[[{"Id": "a"}], inspect_payload("web", 4)]):
            body = exporter.scrape()

        self.assertIn(f"{exporter.COLLECTOR_UP_METRIC} 1", body)
        self.assertIn('docker_container_restart_count{name="web",compose_project="",compose_service=""} 4', body)

    def test_failed_scrape_reports_collector_down_with_no_samples(self):
        with patch("exporter._get_json", side_effect=RuntimeError("proxy unreachable")):
            body = exporter.scrape()

        self.assertIn(f"{exporter.COLLECTOR_UP_METRIC} 0", body)
        self.assertIn("# error: proxy unreachable", body)
        self.assertNotIn(f"{exporter.RESTART_COUNT_METRIC}{{", body)

    def test_recovers_on_next_successful_scrape(self):
        with patch("exporter._get_json", side_effect=RuntimeError("proxy restarting")):
            self.assertIn(f"{exporter.COLLECTOR_UP_METRIC} 0", exporter.scrape())

        with patch("exporter._get_json", side_effect=[[{"Id": "a"}], inspect_payload("web", 4)]):
            recovered_body = exporter.scrape()

        self.assertIn(f"{exporter.COLLECTOR_UP_METRIC} 1", recovered_body)
        self.assertIn('name="web"', recovered_body)


class EscapeLabelValue(unittest.TestCase):
    def test_escapes_backslash_quote_and_newline(self):
        self.assertEqual(exporter.escape_label_value('a\\b"c\nd'), 'a\\\\b\\"c\\nd')

    def test_plain_value_is_unchanged(self):
        self.assertEqual(exporter.escape_label_value("web"), "web")


class FakeDockerHandler(http.server.BaseHTTPRequestHandler):
    routes = {
        "/containers/json": (200, [{"Id": "a1"}]),
        "/containers/a1/json": (200, inspect_payload("web", 9, compose_labels("stack", "web"))),
        "/containers/missing/json": (404, {"message": "No such container"}),
    }

    def do_GET(self):
        status, payload = self.routes.get(self.path, (500, {"message": "unexpected path"}))
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class UnixSocketHTTPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    address_family = socket.AF_UNIX
    allow_reuse_address = False


def serve_in_background(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


class GetJsonOverTcp(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeDockerHandler)
        serve_in_background(self.server)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        host, port = self.server.server_address
        self.base_patch = patch.object(exporter, "DOCKER_API_BASE", f"http://{host}:{port}")
        self.base_patch.start()
        self.addCleanup(self.base_patch.stop)

    def test_lists_and_inspects_containers(self):
        self.assertEqual(
            exporter.collect(),
            ['docker_container_restart_count{name="web",compose_project="stack",compose_service="web"} 9'],
        )

    def test_non_200_raises_with_status(self):
        with self.assertRaises(RuntimeError) as raised:
            exporter._get_json("/containers/missing/json")
        self.assertIn("HTTP 404", str(raised.exception))

    def test_scrape_marks_collector_down_when_nothing_listens(self):
        self.server.shutdown()
        self.server.server_close()
        self.assertIn(f"{exporter.COLLECTOR_UP_METRIC} 0", exporter.scrape())


class GetJsonOverUnixSocket(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        socket_path = os.path.join(self.tempdir.name, "docker.sock")
        self.server = UnixSocketHTTPServer(socket_path, FakeDockerHandler)
        serve_in_background(self.server)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base_patch = patch.object(exporter, "DOCKER_API_BASE", f"unix://{socket_path}")
        self.base_patch.start()
        self.addCleanup(self.base_patch.stop)

    def test_lists_and_inspects_containers(self):
        self.assertEqual(
            exporter.collect(),
            ['docker_container_restart_count{name="web",compose_project="stack",compose_service="web"} 9'],
        )


class OpenConnection(unittest.TestCase):
    def test_http_uses_host_and_port(self):
        connection = exporter.open_connection("http://docker-socket-proxy:2375")
        self.assertEqual((connection.host, connection.port), ("docker-socket-proxy", 2375))

    def test_http_defaults_to_port_80(self):
        self.assertEqual(exporter.open_connection("http://proxy").port, 80)

    def test_unix_uses_the_socket_path(self):
        connection = exporter.open_connection("unix:///var/run/docker.sock")
        self.assertEqual(connection.socket_path, "/var/run/docker.sock")

    def test_other_schemes_are_refused(self):
        for base in ("https://proxy:2376", "tcp://proxy:2375", "/var/run/docker.sock"):
            with self.assertRaises(ValueError):
                exporter.open_connection(base)


if __name__ == "__main__":
    unittest.main()
