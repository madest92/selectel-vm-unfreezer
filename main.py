import argparse
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests
from prometheus_client import (REGISTRY, CollectorRegistry, Gauge,
                               start_http_server)

AUTH_URL = 'https://cloud.api.selcloud.ru/identity/v3/auth/tokens'
REQUEST_TIMEOUT = 10

# Server status when frozen by Selectel
SERVER_FROZEN_STATUS = 'SHELVED_OFFLOADED'
# Only servers with this tag are ever frozen
PREEMPTIBLE_TAG = 'preemptible'
# Auth token refresh interval (24h tokens, refresh earlier to be safe)
TOKEN_UPDATE_INTERVAL = 20 * 60 * 60
# TTL of the auto-discovered region set; unused when --region is given
REGION_CACHE_TTL = 4 * 60 * 60
# Maximum number of threads to poll regions concurrently
MAX_POLL_WORKERS = 10
# Pause after start commands to let servers boot before next poll
SERVER_START_PAUSE = 60
# Order must match Server.labels
METRIC_LABELS = ['server_name', 'server_id', 'region']


@dataclass(frozen=True)
class Server:
    """One Selectel server."""

    id: str
    name: str
    status: str
    region: str
    preemptible: bool = False

    @classmethod
    def from_api(cls, payload: dict, region: str) -> 'Server':
        """Build from a /servers/detail entry."""
        return cls(
            id=payload.get('id') or 'unknown',
            name=payload.get('name') or 'unknown',
            status=payload.get('status') or 'unknown',
            region=region,
            preemptible=PREEMPTIBLE_TAG in (payload.get('tags') or []),
        )

    @property
    def is_frozen(self) -> bool:
        return self.status == SERVER_FROZEN_STATUS

    @property
    def labels(self) -> tuple[str, str, str]:
        return self.name, self.id, self.region

    def __str__(self) -> str:
        return f'{self.name} ({self.region})'


@dataclass(frozen=True)
class Poll:
    """One round of polling: what came back, and from where."""

    servers: list[Server]
    # region -> whether it answered; a failed region reports no servers
    regions: dict[str, bool]


class IgnoreList:
    """Servers that must never be started, matched by name or ID."""

    def __init__(self, entries: set[tuple[str | None, str]]) -> None:
        self._entries = entries

    @classmethod
    def parse(cls, entries: list[str]) -> 'IgnoreList':
        """A bare entry matches any region, `region/name` just that one."""
        parsed = set()
        for entry in entries:
            region, separator, server = entry.partition('/')
            if not separator:
                parsed.add((None, entry))
                continue
            if not region or not server:
                raise ValueError(
                    f'malformed ignore entry {entry!r}, '
                    'expected SERVER or REGION/SERVER',
                )
            parsed.add((region, server))
        return cls(parsed)

    def matches(self, server: Server) -> bool:
        return bool(self._entries & {
            (scope, key)
            for scope in (None, server.region)
            for key in (server.name, server.id)
        })

    def unmonitored_regions(self, regions: list[str]) -> list[str]:
        """Scoped regions we never poll, so those entries can't match."""
        return sorted({
            region for region, _ in self._entries
            if region is not None and region not in regions
        })


class SelectelClient:
    """Thin wrapper around the Selectel Cloud Identity/Compute API."""

    def __init__(
        self,
        account: str,
        project: str,
        user: str,
        password: str,
        logger: logging.Logger,
        regions: list[str] | None = None,
    ) -> None:
        self.account = account
        self.project = project
        self.user = user
        self.password = password
        self.logger = logger
        # Empty means "discover them from the catalog"
        self.regions = regions or []
        self._session = requests.Session()
        self._token: str | None = None
        self._token_updated_at: float = 0.0
        self._endpoints: dict[str, str] = {}
        self._discovered_regions: list[str] = []
        self._discovered_at: float = 0.0

    @staticmethod
    def _compute_endpoints(catalog: list[dict]) -> dict[str, str]:
        """Public compute endpoint per region.

        Regions sit on several domains, so URLs cannot be templated.
        """
        endpoints = {}
        for service in catalog:
            if service.get('type') != 'compute':
                continue
            for endpoint in service.get('endpoints', []):
                if endpoint.get('interface') != 'public':
                    continue
                region = endpoint.get('region_id') or endpoint.get('region')
                url = (endpoint.get('url') or '').rstrip('/')
                if region and url:
                    endpoints[region] = url
        return endpoints

    def _auth_payload(self) -> dict:
        """Password-auth body scoped to the project."""
        domain = {'name': self.account}
        return {
            'auth': {
                'identity': {
                    'methods': ['password'],
                    'password': {
                        'user': {
                            'name': self.user,
                            'domain': domain,
                            'password': self.password,
                        },
                    },
                },
                'scope': {
                    'project': {'name': self.project, 'domain': domain},
                },
            },
        }

    def ensure_token(self) -> None:
        """Refresh the token and endpoint list if the token is stale."""
        token_age = time.time() - self._token_updated_at
        if self._token and token_age < TOKEN_UPDATE_INTERVAL:
            return

        response = self._session.post(
            AUTH_URL, json=self._auth_payload(),
            headers={'Content-Type': 'application/json'},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        self._token = response.headers['X-Subject-Token']
        self._token_updated_at = time.time()
        catalog = response.json().get('token', {}).get('catalog', [])
        self._endpoints = self._compute_endpoints(catalog)
        self.logger.info('Auth token updated')
        self.logger.info(
            'Compute advertised in %s region(s)',
            len(self._endpoints),
        )

    def _get_region_servers(self, region: str) -> list[Server] | None:
        """Servers in one region, or None if it could not be polled."""
        endpoint = self._endpoints.get(region)
        if endpoint is None:
            self.logger.error('No compute endpoint for region %s', region)
            return None
        try:
            response = self._session.get(
                f'{endpoint}/servers/detail',
                headers={
                    'X-Auth-Token': self._token,
                    'OpenStack-API-Version': 'compute latest',
                    },
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code == 401:
                # Token died before its time. Expire it so the next cycle
                # re-authenticates instead of retrying it for hours.
                self._token_updated_at = 0.0
                self.logger.warning(
                    'Auth token rejected in region %s, re-authenticating',
                    region,
                )
                return None
            response.raise_for_status()
        except Exception as e:
            self.logger.error(
                'Failed to fetch servers in region %s: %s', region, e,
            )
            return None
        return [
            Server.from_api(payload, region)
            for payload in response.json().get('servers', [])
        ]

    def _fetch(self, regions: list[str]) -> Poll:
        """Poll the given regions concurrently."""
        if not regions:
            return Poll([], {})
        workers = min(len(regions), MAX_POLL_WORKERS)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(self._get_region_servers, regions))
        servers = [s for group in results if group is not None for s in group]
        self.logger.debug('Servers info: %s', servers)
        return Poll(servers, {
            region: group is not None
            for region, group in zip(regions, results)
        })

    def poll(self) -> Poll:
        """Poll the regions worth polling.

        An explicit --region list is used as-is. Otherwise a periodic
        sweep of every region picks the ones holding preemptible servers.
        """
        if self.regions:
            return self._fetch(self.regions)

        if time.time() - self._discovered_at < REGION_CACHE_TTL:
            return self._fetch(self._discovered_regions)

        poll = self._fetch(sorted(self._endpoints))
        if not any(poll.regions.values()):
            self.logger.warning('Region sweep reached no region, retrying')
            return poll

        # Regions we could not re-check keep their previous verdict.
        unchecked = {
            region for region in self._discovered_regions
            if not poll.regions.get(region)
        }
        self._discovered_regions = sorted(
            {s.region for s in poll.servers if s.preemptible} | unchecked,
        )
        self._discovered_at = time.time()
        self.logger.info(
            'Regions holding preemptible servers: %s',
            ', '.join(self._discovered_regions) or 'none',
        )
        # Narrow this cycle too, or series would appear and vanish
        # every sweep.
        discovered = set(self._discovered_regions)
        return Poll(
            [s for s in poll.servers if s.region in discovered],
            {r: ok for r, ok in poll.regions.items() if r in discovered},
        )

    def start_server(self, server: Server) -> None:
        """Send an "unshelve" action to bring a server back online."""
        endpoint = self._endpoints.get(server.region)
        if endpoint is None:
            raise RuntimeError(
                f'no compute endpoint for region {server.region}',
            )
        response = self._session.post(
            f'{endpoint}/servers/{server.id}/action',
            json={'unshelve': None},
            headers={'X-Auth-Token': self._token}, timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()


class Metrics:
    """The gauges this exporter publishes."""

    def __init__(self, registry: CollectorRegistry = REGISTRY) -> None:
        self.up = Gauge(
            'selectel_server_up',
            'Selectel server status (1 = running, 0 = frozen)',
            METRIC_LABELS, registry=registry,
        )
        self.ignored = Gauge(
            'selectel_server_ignored',
            'Whether the server is excluded from automatic start '
            '(1 = ignored, 0 = managed)',
            METRIC_LABELS, registry=registry,
        )
        self.region_up = Gauge(
            'selectel_region_up',
            'Whether the last poll of the region succeeded '
            '(1 = yes, 0 = the servers below are unknown, not absent)',
            ['region'], registry=registry,
        )

    def export(self, poll: Poll, ignore_list: IgnoreList) -> None:
        """Republish from scratch so vanished servers drop out."""
        self.up.clear()
        self.ignored.clear()
        self.region_up.clear()
        for region, reachable in poll.regions.items():
            self.region_up.labels(region).set(1 if reachable else 0)
        for server in poll.servers:
            self.up.labels(*server.labels).set(0 if server.is_frozen else 1)
            self.ignored.labels(*server.labels).set(
                1 if ignore_list.matches(server) else 0,
            )


class Unfreezer:
    """Polls Selectel, exports metrics and unshelves frozen servers."""

    def __init__(
        self,
        client: SelectelClient,
        metrics: Metrics,
        ignore_list: IgnoreList,
        logger: logging.Logger,
    ) -> None:
        self.client = client
        self.metrics = metrics
        self.ignore_list = ignore_list
        self.logger = logger

    def start_frozen(self, servers: list[Server]) -> int:
        """Start every frozen server not ignored; return how many."""
        started = 0
        for server in servers:
            if not server.is_frozen:
                continue
            if self.ignore_list.matches(server):
                self.logger.info(
                    'Server %s is frozen but ignored, leaving it as is',
                    server,
                )
                continue
            self.logger.warning(
                'Server %s is frozen, trying to start it', server,
            )
            try:
                self.client.start_server(server)
            except Exception as e:
                self.logger.error('Failed to start server %s: %s', server, e)
                continue
            started += 1
        return started

    def poll_once(self) -> None:
        """One cycle: export metrics, then start what is frozen."""
        try:
            self.client.ensure_token()
            poll = self.client.poll()
        except Exception as e:
            self.logger.error('Failed to refresh Selectel state: %s', e)
            return

        self.metrics.export(poll, self.ignore_list)
        if self.start_frozen(poll.servers):
            time.sleep(SERVER_START_PAUSE)

    def run_forever(self, interval: int) -> None:
        """Poll until the process is killed."""
        while True:
            self.poll_once()
            time.sleep(interval)


def env_list(name: str) -> list[str]:
    """Split a comma-separated env var, dropping blanks."""
    return [
        item.strip()
        for item in os.getenv(name, '').split(',')
        if item.strip()
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI args; env vars fill in whatever isn't passed."""
    parser = argparse.ArgumentParser(
        description=(
            'Starts Selectel-frozen servers and exposes Prometheus metrics.'
        ),
    )
    parser.add_argument(
        '--host', default=os.getenv('HOST', '0.0.0.0'),
        help=(
            'Address the metrics HTTP server binds to '
            '(env: HOST, default: 0.0.0.0)'
        ),
    )
    parser.add_argument(
        '--port', type=int, default=int(os.getenv('PORT', 8000)),
        help=(
            'Port the metrics HTTP server listens on '
            '(env: PORT, default: 8000)'
        ),
    )
    parser.add_argument(
        '--account', default=os.getenv('SELECTEL_ACCOUNT'),
        help=(
            'Selectel account (domain) name/ID '
            '(env: SELECTEL_ACCOUNT) [required]'
        ),
    )
    parser.add_argument(
        '--project', default=os.getenv('SELECTEL_PROJECT'),
        help='Selectel project name (env: SELECTEL_PROJECT) [required]',
    )
    parser.add_argument(
        '--user', default=os.getenv('SELECTEL_USER'),
        help='Selectel service user name (env: SELECTEL_USER) [required]',
    )
    parser.add_argument(
        '--password', default=os.getenv('SELECTEL_PASS'),
        help=(
            'Selectel service user password (env: SELECTEL_PASS) '
            '[required]; prefer the env var, this flag is visible via '
            '`ps aux`.'
        ),
    )
    parser.add_argument(
        '--region', dest='regions', action='append', metavar='REGION',
        help=(
            'Selectel Cloud region to monitor, e.g. ru-7, ru-3. Repeat to '
            'watch multiple regions (env: SELECTEL_REGIONS, '
            'comma-separated, default: discover them automatically)'
        ),
    )
    parser.add_argument(
        '--ignore-server', dest='ignored_servers', action='append',
        metavar='[REGION/]SERVER',
        help=(
            'Server name or ID to never start automatically. Prefix with '
            'REGION/, e.g. ru-7/staging-db, to limit to a region. Repeat to '
            'ignore multiple servers (env: SELECTEL_IGNORED_SERVERS)'
        ),
    )
    parser.add_argument(
        '--metric-update-interval', type=int,
        default=int(os.getenv('METRIC_UPDATE_INTERVAL', 10)),
        help=(
            'Seconds between status refresh cycles '
            '(env: METRIC_UPDATE_INTERVAL, default: 10)'
        ),
    )
    parser.add_argument(
        '--log-level', default=os.getenv('LOG_LEVEL', 'info'),
        help=(
            'Logging verbosity: debug, info, warning, error '
            '(env: LOG_LEVEL, default: info)'
        ),
    )

    args = parser.parse_args(argv)
    # No default: an empty list means auto-discovery
    args.regions = args.regions or env_list('SELECTEL_REGIONS')
    args.ignored_servers = (
        args.ignored_servers or env_list('SELECTEL_IGNORED_SERVERS')
    )

    required = {
        '--account / SELECTEL_ACCOUNT': args.account,
        '--project / SELECTEL_PROJECT': args.project,
        '--user / SELECTEL_USER': args.user,
        '--password / SELECTEL_PASS': args.password,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        parser.error(f'missing required parameters: {", ".join(missing)}')

    return args


def configure_logging(level_name: str) -> logging.Logger:
    """Set up logging and return the module logger."""
    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
        format='%(asctime)s[%(levelname)s]: %(message)s',
    )
    return logging.getLogger(__name__)


def log_startup(
    args: argparse.Namespace,
    ignore_list: IgnoreList,
    logger: logging.Logger,
) -> None:
    """Report the effective configuration."""
    logger.info('App started on %s:%s', args.host, args.port)
    logger.info(
        'Regions: %s',
        ', '.join(args.regions) if args.regions else 'auto-discovered',
    )
    if args.ignored_servers:
        logger.info('Ignored servers: %s', ', '.join(args.ignored_servers))
    # Auto-discovery puts every region in scope, so nothing is stray
    stray = (
        ignore_list.unmonitored_regions(args.regions) if args.regions else []
    )
    if stray:
        logger.warning(
            'Ignore list scopes servers to unmonitored region(s): %s; '
            'those entries will never match', ', '.join(stray),
        )
    logger.info(
        'Metric update interval: %s seconds', args.metric_update_interval,
    )


def main() -> None:
    """Wire everything together and start polling."""
    args = parse_args()
    logger = configure_logging(args.log_level)
    ignore_list = IgnoreList.parse(args.ignored_servers)

    client = SelectelClient(
        account=args.account,
        project=args.project,
        user=args.user,
        password=args.password,
        logger=logger,
        regions=args.regions,
    )
    unfreezer = Unfreezer(client, Metrics(), ignore_list, logger)

    start_http_server(args.port, addr=args.host)
    log_startup(args, ignore_list, logger)
    unfreezer.run_forever(args.metric_update_interval)


if __name__ == '__main__':
    main()
