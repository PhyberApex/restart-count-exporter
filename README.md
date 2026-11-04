# restart-count-exporter

A tiny Prometheus exporter for the one crash signal Docker keeps and nothing else exports:
each container's **`RestartCount`**, the number of times the restart policy has respawned it
in place. One Python file, standard library only, served on `:9103/metrics`.

## Why

A container whose entrypoint dies at once and is respawned by `restart: unless-stopped`
keeps its container ID. cAdvisor's `container_start_time_seconds` never moves, `docker ps`
says `Up 2 seconds` forever, and a health check that probes a port sees nothing, because the
process never lives long enough to be probed. The daemon counts every respawn in the
container's inspect payload as `RestartCount`, and that counter is what this exporter reads.

- [cAdvisor](https://github.com/google/cadvisor) exposes start time and resource usage but
  not the restart count.
- `docker ps` / `docker events` show it to a human at a terminal, not to Prometheus.
- **This one** publishes it as `docker_container_restart_count{name=…}` and nothing else,
  so an alert on `increase(...)` catches a crash loop within minutes.

## Metrics

| Metric | Type | Labels | Notes |
|---|---|---|---|
| `docker_container_restart_count` | counter | `name`, `compose_project`, `compose_service` | Docker's `RestartCount`: increments on an in-place respawn, resets to 0 when the container is recreated |
| `docker_container_restart_collector_up` | gauge | | **the gauge to alert on**: 1 when the last pass read every container, 0 otherwise |

`name` is the container name without its leading slash. `compose_project` and
`compose_service` come from the `com.docker.compose.*` labels Compose stamps on every
container it creates, and are empty strings for containers started any other way, so the
label set is the same on every series.

The collector gauge is emitted on every pass, success or failure. When a pass fails (the
socket proxy is down, the daemon is restarting, the socket is not mounted) the body carries
`collector_up 0`, an `# error:` comment with the exception, and **no container samples**, so
an unreachable Docker API is never read back as stale-but-healthy restart counts. The HTTP
server keeps answering throughout, which is exactly why a container health check cannot see
this and the gauge exists.

Sample output:

```text
# HELP docker_container_restart_count Docker's restart-policy respawn count for a container (RestartCount from its inspect payload): increments on an in-place respawn, resets only when the container is recreated.
# TYPE docker_container_restart_count counter
docker_container_restart_count{name="web-1",compose_project="shop",compose_service="web"} 0
docker_container_restart_count{name="worker-1",compose_project="shop",compose_service="worker"} 14
docker_container_restart_count{name="portainer",compose_project="",compose_service=""} 0
# HELP docker_container_restart_collector_up Whether the last scrape collected restart counts successfully (1) or failed (0).
# TYPE docker_container_restart_collector_up gauge
docker_container_restart_collector_up 1
```

## Running

The image is published to GHCR for `linux/amd64` and `linux/arm64`, so it runs on a
Raspberry Pi as it does on a server. Tags: `:latest` and `:x.y.z` for releases, `:next` for
the current `main`.

The exporter needs to reach the Docker Engine API. There are two ways, and the first is the
one to prefer.

### Through docker-socket-proxy (recommended)

The exporter only ever issues `GET /containers/json` and `GET /containers/{id}/json`. Put
[docker-socket-proxy](https://github.com/Tecnativa/docker-socket-proxy) between it and the
socket with `CONTAINERS=1` as the only permission and the exporter holds none of the
socket's power to start, stop, exec into or build anything, whatever happens to it:

```yaml
services:
  docker-socket-proxy:
    image: ghcr.io/tecnativa/docker-socket-proxy:v0.5.0
    restart: unless-stopped
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      - CONTAINERS=1
      - POST=0

  restart-count-exporter:
    image: ghcr.io/phyberapex/restart-count-exporter:latest
    restart: unless-stopped
    ports:
      - "9103:9103"
    environment:
      - DOCKER_API_BASE=http://docker-socket-proxy:2375
    depends_on:
      - docker-socket-proxy
```

Do not publish the proxy's port 2375 unless you mean to: everything on your network could
then list your containers.

### Straight from the socket

If you would rather not run a proxy, mount the socket read-only and point the exporter at it:

```yaml
services:
  restart-count-exporter:
    image: ghcr.io/phyberapex/restart-count-exporter:latest
    restart: unless-stopped
    ports:
      - "9103:9103"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      - DOCKER_API_BASE=unix:///var/run/docker.sock
```

The `:ro` mount only protects the socket file itself. Anything that can talk to the socket
can do everything the daemon can, including starting a privileged container, so this grants
the exporter root-equivalent access to the host. The code only ever reads two endpoints, and
you can verify that in a minute, but the blast radius of a bug would be the whole machine.
That is why the proxy is the recommended setup.

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `9103` | TCP port the `/metrics` server binds on `0.0.0.0` |
| `SCRAPE_INTERVAL_S` | `5` | Seconds between collection passes |
| `DOCKER_API_BASE` | `http://docker-socket-proxy:2375` | Where the Docker Engine API is: `http://host:port` or `unix:///path/to/docker.sock` |

Collection runs on its own thread independent of Prometheus. A pass is one list call plus
one inspect call per container; with a few dozen containers every five seconds it is well
under 1 % of one CPU core.

### Prometheus

```yaml
scrape_configs:
  - job_name: restart-count
    static_configs:
      - targets: ['docker-host:9103']
```

### Alerting

Alert on the counter moving, and on the collector, not on the container:

```yaml
groups:
  - name: docker
    rules:
      - alert: ContainerCrashLooping
        expr: increase(docker_container_restart_count[1h]) > 2
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: '{{ $labels.name }} has restarted {{ $value | humanize }} times in the last hour'
      - alert: RestartCollectorDown
        expr: docker_container_restart_collector_up == 0
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: restart-count-exporter cannot reach the Docker API on {{ $labels.instance }}
      - alert: RestartCollectorAbsent
        expr: absent(docker_container_restart_collector_up)
        for: 5m
        labels:
          severity: warning
```

`increase()` rather than the raw value, because the count resets to zero when a container is
recreated (every deploy), and a container that restarted twelve times last month is not news.

### Grafana

```promql
# Restarts in the last hour, per container
increase(docker_container_restart_count[1h])

# Restarts per Compose project
sum by (compose_project) (increase(docker_container_restart_count[1h]))
```

## Running outside Docker

```bash
DOCKER_API_BASE=unix:///var/run/docker.sock python exporter.py
```

Nothing to install: the exporter imports only the standard library.

## Developing

```bash
python -m unittest discover -p 'test_*.py' -v
```

The tests stub the Docker API in-process and also start a fake one on a loopback port and
on a unix socket, so they run anywhere without a Docker daemon. CI runs them on every pull
request, builds the image, and scrapes the runner's own daemon through it.

## License

[MIT](LICENSE)
