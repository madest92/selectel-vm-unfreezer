import argparse
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from prometheus_client import Gauge, start_http_server

AUTH_URL = 'https://cloud.api.selcloud.ru/identity/v3/auth/tokens'
REQUEST_TIMEOUT = 10

# Auth token refresh interval (24h tokens, refresh earlier to be safe)
TOKEN_UPDATE_INTERVAL = 20 * 60 * 60
# Server status when frozen by Selectel
SERVER_FROZEN_STATUS = 'SHELVED_OFFLOADED'
# Pause after start commands to let servers boot before next poll
SERVER_START_PAUSE = 60


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
            'comma-separated, default: ru-7)'
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

    if not args.regions:
        env_regions = os.getenv('SELECTEL_REGIONS', 'ru-7')
        args.regions = [r.strip() for r in env_regions.split(',') if r.strip()]

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
    """Set up root logging format/level and return the module logger."""
    logging.basicConfig(
        level=getattr(logging, level_name.upper(), logging.INFO),
        format='%(asctime)s[%(levelname)s]: %(message)s',
    )
    return logging.getLogger(__name__)


class SelectelClient:
    """Thin wrapper around the Selectel Cloud Identity/Compute API."""

    def __init__(
        self,
        account: str,
        project: str,
        user: str,
        password: str,
        regions: list[str],
        logger: logging.Logger,
    ) -> None:
        self.account = account
        self.project = project
        self.user = user
        self.password = password
        self.regions = regions
        self.logger = logger
        self._token: str | None = None
        self._token_updated_at: float = 0.0

    @staticmethod
    def _compute_url(region: str) -> str:
        """Base URL of the Compute API for the given region."""
        return f'https://{region}.cloud.api.selcloud.ru/compute/v2.1'

    def ensure_token(self) -> None:
        """Get a fresh auth token if there isn't one yet or it expired."""
        token_age = time.time() - self._token_updated_at
        if self._token and token_age < TOKEN_UPDATE_INTERVAL:
            return

        payload = {
            'auth': {
                'identity': {
                    'methods': ['password'],
                    'password': {
                        'user': {
                            'name': self.user,
                            'domain': {'name': self.account},
                            'password': self.password,
                        },
                    },
                },
                'scope': {
                    'project': {
                        'name': self.project,
                        'domain': {'name': self.account},
                    },
                },
            },
        }
        response = requests.post(
            AUTH_URL, json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        self._token = response.headers['X-Subject-Token']
        self._token_updated_at = time.time()
        self.logger.info('Auth token updated')

    def _get_region_servers(self, region: str) -> list[dict]:
        """Fetch id/name/status for every server in a single region."""
        url = f'{self._compute_url(region)}/servers/detail'
        try:
            response = requests.get(
                url,
                headers={'X-Auth-Token': self._token},
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
        except Exception as e:
            self.logger.error(
                'Failed to fetch servers in region %s: %s', region, e,
            )
            return []
        return [
            {
                'id': s.get('id'),
                'name': s.get('name'),
                'status': s.get('status'),
                'region': region,
            }
            for s in response.json().get('servers', [])
        ]

    def get_servers(self) -> list[dict]:
        """Return id/name/status/region for every server, all regions
        polled concurrently."""
        with ThreadPoolExecutor(max_workers=len(self.regions)) as pool:
            per_region = pool.map(self._get_region_servers, self.regions)
        all_servers = [server for servers in per_region for server in servers]
        self.logger.debug('Servers info: %s', all_servers)
        return all_servers

    def start_server(self, region: str, server_id: str) -> None:
        """Send an "unshelve" action to bring a frozen server back online."""
        url = f'{self._compute_url(region)}/servers/{server_id}/action'
        response = requests.post(
            url, json={'unshelve': None},
            headers={'X-Auth-Token': self._token}, timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()


def start_frozen_servers(
    client: SelectelClient,
    servers: list[dict],
    logger: logging.Logger,
) -> None:
    """Start every server in SERVER_FROZEN_STATUS; pause if any started."""
    started_any = False
    for server in servers:
        if server.get('status') != SERVER_FROZEN_STATUS:
            continue
        server_name = server.get('name', 'unknown')
        region = server.get('region')
        logger.warning(
            'Server %s (%s) is frozen, trying to start it',
            server_name, region,
        )
        try:
            client.start_server(region, server['id'])
        except Exception as e:
            logger.error(
                'Failed to start server %s (%s): %s', server_name, region, e,
            )
            continue
        started_any = True

    if started_any:
        time.sleep(SERVER_START_PAUSE)


def export_metrics(gauge: Gauge, servers: list[dict]) -> None:
    """Publish server statuses as a gauge (1 = running, 0 = frozen)."""
    gauge.clear()
    for server in servers:
        server_name = server.get('name', 'unknown')
        region = server.get('region', 'unknown')
        is_frozen = server.get('status') == SERVER_FROZEN_STATUS
        gauge.labels(server_name, region).set(0 if is_frozen else 1)


def main() -> None:
    """Start the metrics HTTP server and continuously poll/repair servers."""
    args = parse_args()
    logger = configure_logging(args.log_level)

    server_up = Gauge(
        'selectel_server_up',
        'Selectel server status (1 = running, 0 = frozen)',
        ['server_name', 'region'],
    )
    client = SelectelClient(
        account=args.account,
        project=args.project,
        user=args.user,
        password=args.password,
        regions=args.regions,
        logger=logger,
    )

    start_http_server(args.port, addr=args.host)
    logger.info('App started on %s:%s', args.host, args.port)
    logger.info('Regions: %s', ', '.join(args.regions))
    logger.info(
        'Metric update interval: %s seconds', args.metric_update_interval,
    )

    while True:
        try:
            client.ensure_token()
            servers = client.get_servers()
        except Exception as e:
            logger.error('Failed to refresh Selectel state: %s', e)
            time.sleep(args.metric_update_interval)
            continue

        export_metrics(server_up, servers)
        start_frozen_servers(client, servers, logger)
        time.sleep(args.metric_update_interval)


if __name__ == '__main__':
    main()
