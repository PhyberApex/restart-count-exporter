"""Prometheus exporter for Docker's native per-container restart count.

Reads container-inspect payloads from the Docker Engine API, either through
docker-socket-proxy (the default, so the exporter carries none of the socket's
write or exec permissions) or straight from a mounted unix socket.
"""

import http.client
import http.server
import json
import os
import socket
import threading
import time
from urllib.parse import urlsplit

PORT = int(os.environ.get("PORT", "9103"))
SCRAPE_INTERVAL_S = float(os.environ.get("SCRAPE_INTERVAL_S", "5"))
DOCKER_API_BASE = os.environ.get("DOCKER_API_BASE", "http://docker-socket-proxy:2375")
REQUEST_TIMEOUT_S = 5

RESTART_COUNT_METRIC = "docker_container_restart_count"
RESTART_COUNT_HELP = (
    "Docker's restart-policy respawn count for a container (RestartCount from "
    "its inspect payload): increments on an in-place respawn, resets only "
    "when the container is recreated."
)
COLLECTOR_UP_METRIC = "docker_container_restart_collector_up"
COLLECTOR_UP_HELP = "Whether the last scrape collected restart counts successfully (1) or failed (0)."

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"

metrics_output = ""


def escape_label_value(value):
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class _UnixSocketConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def open_connection(api_base, timeout=REQUEST_TIMEOUT_S):
    """Build an HTTP connection to the Docker API named by `api_base`.

    Accepts `http://host:port` (a TCP endpoint such as docker-socket-proxy)
    or `unix:///path/to/docker.sock` (the daemon's own socket).
    """
    url = urlsplit(api_base)
    if url.scheme == "unix":
        return _UnixSocketConnection(url.path, timeout)
    if url.scheme == "http":
        return http.client.HTTPConnection(url.hostname, url.port or 80, timeout=timeout)
    raise ValueError(f"DOCKER_API_BASE must start with http:// or unix://, got {api_base!r}")


def _get_json(path):
    connection = open_connection(DOCKER_API_BASE)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise RuntimeError(f"GET {path} returned HTTP {response.status}: {body[:200].decode(errors='replace')}")
        return json.loads(body)
    finally:
        connection.close()


def sample_line(inspect):
    labels = inspect.get("Config", {}).get("Labels") or {}
    label_pairs = (
        ("name", inspect["Name"].lstrip("/")),
        ("compose_project", labels.get(COMPOSE_PROJECT_LABEL, "")),
        ("compose_service", labels.get(COMPOSE_SERVICE_LABEL, "")),
    )
    rendered_labels = ",".join(f'{key}="{escape_label_value(value)}"' for key, value in label_pairs)
    return f"{RESTART_COUNT_METRIC}{{{rendered_labels}}} {inspect['RestartCount']}"


def collect():
    """Return one restart-count sample line per container the API lists.

    Raises whatever the Docker API request raises; callers decide how to
    render a failure.
    """
    return [sample_line(_get_json(f"/containers/{container['Id']}/json")) for container in _get_json("/containers/json")]


def render(samples, collector_up, error=None):
    lines = []
    if error is not None:
        lines.append(f"# error: {error}")
    lines.append(f"# HELP {RESTART_COUNT_METRIC} {RESTART_COUNT_HELP}")
    lines.append(f"# TYPE {RESTART_COUNT_METRIC} counter")
    lines.extend(samples)
    lines.append(f"# HELP {COLLECTOR_UP_METRIC} {COLLECTOR_UP_HELP}")
    lines.append(f"# TYPE {COLLECTOR_UP_METRIC} gauge")
    lines.append(f"{COLLECTOR_UP_METRIC} {collector_up}")
    return "\n".join(lines) + "\n"


def scrape():
    """Run one collection pass and return the exposition body for it.

    A failure renders collector_up=0 with no samples, so an unreachable Docker
    API is never read back as stale-but-healthy restart counts.
    """
    try:
        return render(collect(), 1)
    except Exception as e:
        return render([], 0, error=e)


def scrape_loop():
    global metrics_output
    while True:
        metrics_output = scrape()
        time.sleep(SCRAPE_INTERVAL_S)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(metrics_output.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    open_connection(DOCKER_API_BASE).close()
    threading.Thread(target=scrape_loop, daemon=True).start()
    time.sleep(0.5)
    print(f"Serving metrics on :{PORT}/metrics, reading Docker at {DOCKER_API_BASE}", flush=True)
    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    server.serve_forever()
