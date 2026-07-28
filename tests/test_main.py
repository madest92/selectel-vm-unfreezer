import time
from unittest.mock import Mock

import pytest
import requests

import main
from main import IgnoreList, Poll, Server

FROZEN = main.SERVER_FROZEN_STATUS


def sample(registry, metric: str, server: Server):
    """Value of `metric` for one server, or None if not exported."""
    return registry.get_sample_value(metric, {
        'server_name': server.name,
        'server_id': server.id,
        'region': server.region,
    })


def unfreezer(logger, ignored=(), client=None):
    return main.Unfreezer(
        client or Mock(), Mock(), IgnoreList.parse(list(ignored)), logger,
    )


def parse_args(*argv):
    return main.parse_args([
        '--account', 'acct', '--project', 'proj',
        '--user', 'usr', '--password', 'pwd',
        *argv,
    ])


@pytest.mark.parametrize(
    'argv, env, expected',
    [
        pytest.param([], None, [], id='empty_means_auto_discovery'),
        pytest.param([], '  ,, ', [], id='blank_env_means_auto_discovery'),
        pytest.param(
            ['--region', 'ru-1'], 'ru-3, ru-9', ['ru-1'],
            id='flag_is_split_from_env_and_wins',
        ),
    ],
)
def test_parse_args_regions(monkeypatch, argv, env, expected):
    if env is None:
        monkeypatch.delenv('SELECTEL_REGIONS', raising=False)
    else:
        monkeypatch.setenv('SELECTEL_REGIONS', env)

    assert parse_args(*argv).regions == expected


def test_parse_args_reads_the_ignore_env_var(monkeypatch):
    monkeypatch.setenv('SELECTEL_IGNORED_SERVERS', 'a, b')

    assert parse_args().ignored_servers == ['a', 'b']


@pytest.mark.parametrize(
    'token, age_offset, expect_refresh',
    [
        pytest.param('existing', 0, False, id='fresh_token_skips_refresh'),
        pytest.param(
            'old', main.TOKEN_UPDATE_INTERVAL + 1, True,
            id='expired_token_refreshes',
        ),
        pytest.param(None, 0, True, id='missing_token_fetches'),
    ],
)
def test_ensure_token(client, monkeypatch, token, age_offset, expect_refresh):
    client._token = token
    client._token_updated_at = time.time() - age_offset
    mock_post = Mock(return_value=Mock(
        headers={'X-Subject-Token': 'new-tok'},
        json=Mock(return_value={'token': {'catalog': []}}),
    ))
    monkeypatch.setattr(client._session, 'post', mock_post)

    client.ensure_token()

    assert mock_post.called is expect_refresh
    if not expect_refresh:
        assert client._token == token
        return

    assert client._token == 'new-tok'
    # Keystone nests the account under both the user and the scope.
    payload = mock_post.call_args.kwargs['json']['auth']
    assert payload['identity']['password']['user'] == {
        'name': 'usr', 'domain': {'name': 'acct'}, 'password': 'pwd',
    }
    assert payload['scope']['project'] == {
        'name': 'proj', 'domain': {'name': 'acct'},
    }


def test_compute_endpoints_keeps_public_urls_per_region():
    # Shapes from the real catalog: several interfaces per region,
    # two domains, trailing slashes.
    catalog = [
        {'type': 'identity', 'endpoints': [
            {'region_id': 'ru-7', 'interface': 'public', 'url': 'https://i/'},
        ]},
        {'type': 'compute', 'endpoints': [
            {
                'region_id': 'ru-7', 'interface': 'public',
                'url': 'https://ru-7.cloud.api.selcloud.ru/compute/v2.1/',
            },
            {
                'region_id': 'ru-7', 'interface': 'admin',
                'url': 'https://admin-ru-7.cloud.api.selcloud.ru:13774/v2.1/',
            },
            {
                'region_id': 'kz-1', 'interface': 'public',
                'url': 'https://kz-1.cloud.api.servercore.com/compute/v2.1/',
            },
        ]},
    ]

    assert main.SelectelClient._compute_endpoints(catalog) == {
        'ru-7': 'https://ru-7.cloud.api.selcloud.ru/compute/v2.1',
        'kz-1': 'https://kz-1.cloud.api.servercore.com/compute/v2.1',
    }


@pytest.mark.parametrize(
    'get_side_effect, get_json, expected',
    [
        pytest.param(
            None,
            {'servers': [
                {'id': '1', 'name': 'srv-1', 'status': 'ACTIVE'},
                {
                    'id': '2', 'name': 'srv-2', 'status': FROZEN,
                    'tags': ['preemptible', 'Standard Line'],
                },
            ]},
            [
                Server('1', 'srv-1', 'ACTIVE', 'ru-1'),
                Server('2', 'srv-2', FROZEN, 'ru-1', preemptible=True),
            ],
            id='parses_servers_and_preemptible_tag',
        ),
        pytest.param(
            requests.ConnectionError('down'), None, None,
            id='failure_is_none_not_an_empty_list',
        ),
    ],
)
def test_get_region_servers(
    client, monkeypatch, get_side_effect, get_json, expected,
):
    client._token = 'tok'
    if get_side_effect is not None:
        mock_get = Mock(side_effect=get_side_effect)
    else:
        mock_get = Mock(return_value=Mock(json=Mock(return_value=get_json)))
    monkeypatch.setattr(client._session, 'get', mock_get)

    assert client._get_region_servers('ru-1') == expected


def test_get_region_servers_expires_a_rejected_token(client, monkeypatch):
    client._token = 'stale'
    client._token_updated_at = time.time()
    monkeypatch.setattr(
        client._session, 'get', Mock(return_value=Mock(status_code=401)),
    )

    assert client._get_region_servers('ru-1') is None
    # Zeroed, so ensure_token() fetches a new one on the next cycle.
    assert client._token_updated_at == 0


def test_get_region_servers_asks_for_a_microversion_with_tags(
    client, monkeypatch,
):
    client._token = 'tok'
    mock_get = Mock(return_value=Mock(json=Mock(return_value={'servers': []})))
    monkeypatch.setattr(client._session, 'get', mock_get)

    client._get_region_servers('ru-1')

    # Without it Nova omits `tags` and every server looks non-preemptible.
    headers = mock_get.call_args.kwargs['headers']
    assert headers['OpenStack-API-Version'] == 'compute latest'


def test_start_server_posts_unshelve_action(client, monkeypatch):
    client._token = 'tok'
    mock_response = Mock()
    mock_post = Mock(return_value=mock_response)
    monkeypatch.setattr(client._session, 'post', mock_post)

    client.start_server(Server('srv-id', 'srv', 'ACTIVE', 'ru-1'))

    assert mock_post.call_args.args[0] == (
        'https://ru-1.cloud.api.selcloud.ru/compute/v2.1/servers/srv-id/action'
    )
    assert mock_post.call_args.kwargs['json'] == {'unshelve': None}
    mock_response.raise_for_status.assert_called_once()


def test_poll_narrows_to_preemptible_regions_and_caches(client, monkeypatch):
    client.regions = []  # no --region, so discovery is in charge
    client._endpoints = {r: f'https://{r}' for r in ('ru-1', 'ru-2', 'ru-3')}
    per_region = {
        'ru-1': [Server('1', 'a', 'ACTIVE', 'ru-1', preemptible=True)],
        'ru-2': [Server('2', 'b', 'ACTIVE', 'ru-2')],
        'ru-3': [],
    }
    polled = []

    def fake_fetch(region):
        polled.append(region)
        return per_region[region]

    monkeypatch.setattr(client, '_get_region_servers', fake_fetch)

    # First call sweeps everything, then narrows the result to ru-1.
    poll = client.poll()
    assert sorted(polled) == ['ru-1', 'ru-2', 'ru-3']
    assert [server.region for server in poll.servers] == ['ru-1']
    assert poll.regions == {'ru-1': True}

    # Within the TTL only the region that matters is polled again.
    polled.clear()
    assert [s.region for s in client.poll().servers] == ['ru-1']
    assert polled == ['ru-1']

    # Once the cache expires the full sweep happens again.
    polled.clear()
    client._discovered_at -= main.REGION_CACHE_TTL
    client.poll()
    assert sorted(polled) == ['ru-1', 'ru-2', 'ru-3']


def test_poll_survives_a_failing_sweep(client, monkeypatch):
    client.regions = []
    client._endpoints = {r: f'https://{r}' for r in ('ru-1', 'ru-2')}
    unreachable = set()

    def fake_fetch(region):
        if region in unreachable:
            return None
        return [Server('1', 'a', FROZEN, region, preemptible=True)]

    monkeypatch.setattr(client, '_get_region_servers', fake_fetch)
    client.poll()
    assert client._discovered_regions == ['ru-1', 'ru-2']

    # Reaching nothing proves nothing, so the set must survive untouched
    # instead of being blanked for a whole REGION_CACHE_TTL.
    unreachable = {'ru-1', 'ru-2'}
    client._discovered_at -= main.REGION_CACHE_TTL
    assert client.poll().servers == []
    assert client._discovered_regions == ['ru-1', 'ru-2']

    # Half a sweep keeps the half it could not re-check.
    unreachable = {'ru-1'}
    client._discovered_at -= main.REGION_CACHE_TTL
    client.poll()
    assert client._discovered_regions == ['ru-1', 'ru-2']


def test_poll_uses_explicit_regions_without_sweeping(client, monkeypatch):
    polled = []

    def fake_fetch(region):
        polled.append(region)
        return []

    monkeypatch.setattr(client, '_get_region_servers', fake_fetch)
    client._endpoints = {r: f'https://{r}' for r in ('ru-1', 'ru-2', 'ru-9')}

    client.poll()
    client.poll()

    # ru-9 is advertised but never swept: discovery is auto-mode only.
    assert polled == ['ru-1', 'ru-2', 'ru-1', 'ru-2']
    assert client._discovered_regions == []
    assert client._discovered_at == 0.0


@pytest.mark.parametrize(
    'entry',
    [
        pytest.param('ru-7/', id='missing_server'),
        pytest.param('/srv-1', id='missing_region'),
    ],
)
def test_ignore_list_rejects_malformed_entries(entry):
    with pytest.raises(ValueError, match='malformed ignore entry'):
        IgnoreList.parse([entry])


@pytest.mark.parametrize(
    'entries, expected',
    [
        pytest.param([], False, id='empty_list_ignores_nothing'),
        pytest.param(['srv-1'], True, id='matches_by_name'),
        pytest.param(['id-1'], True, id='matches_by_id'),
        pytest.param(['srv-2'], False, id='other_server_does_not'),
        pytest.param(['ru-1/srv-1'], True, id='matches_scoped_name'),
        pytest.param(['ru-2/srv-1'], False, id='scope_to_other_region_misses'),
    ],
)
def test_ignore_list_matches(entries, expected):
    server = Server('id-1', 'srv-1', 'ACTIVE', 'ru-1')

    assert IgnoreList.parse(entries).matches(server) is expected


@pytest.mark.parametrize(
    'servers, ignored, start_side_effect, attempted_ids, expected_started',
    [
        pytest.param(
            [
                Server('1', 'a', 'ACTIVE', 'ru-1'),
                Server('2', 'b', FROZEN, 'ru-1'),
            ],
            [], None, ['2'], 1,
            id='starts_only_frozen',
        ),
        pytest.param(
            [
                Server('1', 'a', FROZEN, 'ru-1'),
                Server('2', 'b', FROZEN, 'ru-2'),
            ],
            [], [Exception('boom'), None], ['1', '2'], 1,
            id='failed_start_is_attempted_but_not_counted',
        ),
        pytest.param(
            [
                Server('1', 'a', FROZEN, 'ru-1'),
                Server('2', 'b', FROZEN, 'ru-2'),
            ],
            ['a'], None, ['2'], 1,
            id='skips_ignored_server_but_starts_others',
        ),
    ],
)
def test_start_frozen(
    logger, servers, ignored, start_side_effect, attempted_ids,
    expected_started,
):
    client = Mock()
    if start_side_effect is not None:
        client.start_server.side_effect = start_side_effect
    unf = unfreezer(logger, ignored=ignored, client=client)

    started = unf.start_frozen(
        Poll(servers, {s.region: True for s in servers}),
    )

    calls = client.start_server.call_args_list
    assert [call.args[0].id for call in calls] == attempted_ids
    assert started == expected_started


def test_start_frozen_reports_a_state_only_once(caplog, logger):
    frozen = Server('1', 'db', FROZEN, 'ru-1')
    running = Server('1', 'db', 'ACTIVE', 'ru-1')
    healthy = Server('2', 'web', 'ACTIVE', 'ru-1')
    unf = unfreezer(logger, ignored=['db'])

    with caplog.at_level('DEBUG'):
        for _ in range(5):
            unf.start_frozen(Poll([frozen, healthy], {'ru-1': True}))
        # A server that was never frozen is not an event worth reporting.
        assert [r.message for r in caplog.records] == [
            'Server db (ru-1) is frozen, but ignored',
        ]
        assert caplog.records[0].levelname == 'WARNING'

        # The user unfreezes it by hand: that transition must show up.
        caplog.clear()
        for _ in range(2):
            unf.start_frozen(Poll([running, healthy], {'ru-1': True}))
        assert [r.message for r in caplog.records] == [
            'Server db (ru-1) is running again',
        ]

        # ...and so must freezing it again.
        caplog.clear()
        unf.start_frozen(Poll([frozen, healthy], {'ru-1': True}))
        assert len(caplog.records) == 1


def test_start_frozen_repeats_a_failure_only_when_it_changes(caplog, logger):
    client = Mock()
    client.start_server.side_effect = RuntimeError('quota exceeded')
    poll = Poll([Server('1', 'db', FROZEN, 'ru-1')], {'ru-1': True})
    unf = unfreezer(logger, client=client)

    with caplog.at_level('DEBUG'):
        for _ in range(3):
            unf.start_frozen(poll)
        assert len(caplog.records) == 1

        caplog.clear()
        client.start_server.side_effect = RuntimeError('network down')
        unf.start_frozen(poll)

    # A different failure is a new fact, so it gets through.
    assert len(caplog.records) == 1
    assert 'network down' in caplog.records[0].message
    # Retries never stopped, only the logging did.
    assert client.start_server.call_count == 4


def test_start_frozen_keeps_memory_of_unreachable_regions(caplog, logger):
    frozen = Server('1', 'db', FROZEN, 'ru-1')
    unf = unfreezer(logger, ignored=['db'])

    with caplog.at_level('DEBUG'):
        unf.start_frozen(Poll([frozen], {'ru-1': True}))
        caplog.clear()

        # ru-1 goes dark, so its servers are absent from the poll. That is
        # not a deletion, and must not re-announce them once it returns.
        unf.start_frozen(Poll([], {'ru-1': False}))
        unf.start_frozen(Poll([frozen], {'ru-1': True}))

    assert caplog.records == []


def test_poll_once_pauses_only_when_something_started(monkeypatch, logger):
    client = Mock()
    client.poll.return_value = Poll(
        [Server('1', 'a', FROZEN, 'ru-1')], {'ru-1': True},
    )
    sleep_mock = Mock()
    monkeypatch.setattr(main.time, 'sleep', sleep_mock)
    unf = unfreezer(logger, client=client)

    unf.poll_once()
    assert sleep_mock.call_args.args == (main.SERVER_START_PAUSE,)

    sleep_mock.reset_mock()
    client.poll.return_value = Poll(
        [Server('1', 'a', 'ACTIVE', 'ru-1')], {'ru-1': True},
    )
    unf.poll_once()
    assert sleep_mock.called is False


def test_poll_once_keeps_metrics_when_selectel_is_unreachable(logger):
    client = Mock()
    client.poll.side_effect = requests.ConnectionError('down')
    metrics = Mock()
    unf = main.Unfreezer(client, metrics, IgnoreList.parse([]), logger)

    unf.poll_once()

    # Exporting here would clear every series instead of leaving the last
    # known state in place.
    assert metrics.export.called is False
    assert client.start_server.called is False


def test_metrics_export_replaces_previous_samples(metrics, registry):
    old = Server('a1', 'srv-1', 'ACTIVE', 'ru-1')
    new = Server('a2', 'srv-2', FROZEN, 'ru-1')

    metrics.export(Poll([old], {'ru-1': True}), IgnoreList.parse([]))
    assert sample(registry, 'selectel_server_up', old) == 1

    metrics.export(Poll([new], {'ru-1': True}), IgnoreList.parse([]))
    assert sample(registry, 'selectel_server_up', old) is None
    assert sample(registry, 'selectel_server_up', new) == 0


def test_metrics_export_sets_every_gauge(metrics, registry):
    managed = Server('1', 'srv-1', 'ACTIVE', 'ru-1')
    ignored = Server('2', 'srv-2', FROZEN, 'ru-1')

    metrics.export(
        Poll([managed, ignored], {'ru-1': True, 'ru-2': False}),
        IgnoreList.parse(['srv-2']),
    )

    assert sample(registry, 'selectel_server_up', managed) == 1
    assert sample(registry, 'selectel_server_ignored', managed) == 0
    # An ignored server still reports its real status.
    assert sample(registry, 'selectel_server_up', ignored) == 0
    assert sample(registry, 'selectel_server_ignored', ignored) == 1
    for region, expected in (('ru-1', 1), ('ru-2', 0)):
        assert registry.get_sample_value(
            'selectel_region_up', {'region': region},
        ) == expected
