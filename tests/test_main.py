import time
from unittest.mock import Mock

import pytest
import requests

import main


@pytest.mark.parametrize(
    'argv, env, expected',
    [
        pytest.param([], None, [], id='defaults_to_empty_list'),
        pytest.param([], ' a , b ,, ', ['a', 'b'], id='env_is_comma_split'),
        pytest.param(
            ['--ignore-server', 'a', '--ignore-server', 'b'], 'c',
            ['a', 'b'], id='flag_is_repeatable_and_wins_over_env',
        ),
    ],
)
def test_parse_args_ignored_servers(monkeypatch, argv, env, expected):
    if env is None:
        monkeypatch.delenv('SELECTEL_IGNORED_SERVERS', raising=False)
    else:
        monkeypatch.setenv('SELECTEL_IGNORED_SERVERS', env)

    args = main.parse_args([
        '--account', 'acct', '--project', 'proj',
        '--user', 'usr', '--password', 'pwd',
        *argv,
    ])

    assert args.ignored_servers == expected


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
    mock_post = Mock(return_value=Mock(headers={'X-Subject-Token': 'new-tok'}))
    monkeypatch.setattr(main.requests, 'post', mock_post)

    client.ensure_token()

    assert mock_post.called is expect_refresh
    if not expect_refresh:
        assert client._token == token
        return

    assert client._token == 'new-tok'
    payload = mock_post.call_args.kwargs['json']
    user = payload['auth']['identity']['password']['user']
    assert user['name'] == 'usr'
    assert user['domain']['name'] == 'acct'
    assert user['password'] == 'pwd'
    assert payload['auth']['scope']['project']['name'] == 'proj'


@pytest.mark.parametrize(
    'get_side_effect, get_json, expected',
    [
        pytest.param(
            None,
            {'servers': [
                {'id': '1', 'name': 'srv-1', 'status': 'ACTIVE'},
                {'id': '2', 'name': 'srv-2', 'status': 'SHELVED_OFFLOADED'},
            ]},
            [
                {
                    'id': '1', 'name': 'srv-1', 'status': 'ACTIVE',
                    'region': 'ru-1',
                },
                {
                    'id': '2', 'name': 'srv-2', 'status': 'SHELVED_OFFLOADED',
                    'region': 'ru-1',
                },
            ],
            id='parses_servers',
        ),
        pytest.param(
            requests.ConnectionError('down'), None, [],
            id='network_error_returns_empty_list',
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
    monkeypatch.setattr(main.requests, 'get', mock_get)

    servers = client._get_region_servers('ru-1')

    assert servers == expected


def test_start_server_posts_unshelve_action(client, monkeypatch):
    client._token = 'tok'
    mock_response = Mock()
    mock_post = Mock(return_value=mock_response)
    monkeypatch.setattr(main.requests, 'post', mock_post)

    client.start_server('ru-1', 'srv-id')

    assert mock_post.call_args.args[0] == (
        'https://ru-1.cloud.api.selcloud.ru/compute/v2.1/servers/srv-id/action'
    )
    assert mock_post.call_args.kwargs['json'] == {'unshelve': None}
    mock_response.raise_for_status.assert_called_once()


def test_parse_ignore_list_splits_on_first_slash_only():
    assert main.parse_ignore_list(['ru-7/team/srv-1']) == {
        ('ru-7', 'team/srv-1'),
    }


@pytest.mark.parametrize(
    'entry',
    [
        pytest.param('ru-7/', id='missing_server'),
        pytest.param('/srv-1', id='missing_region'),
    ],
)
def test_parse_ignore_list_rejects_malformed_entries(entry):
    with pytest.raises(ValueError, match='malformed ignore entry'):
        main.parse_ignore_list([entry])


@pytest.mark.parametrize(
    'entries, expected',
    [
        pytest.param([], False, id='empty_list_ignores_nothing'),
        pytest.param(['srv-1'], True, id='matches_by_name'),
        pytest.param(['id-1'], True, id='matches_by_id'),
        pytest.param(['srv-2', 'id-2'], False, id='other_server_does_not'),
        pytest.param(['ru-1'], False, id='bare_region_is_not_matched'),
        pytest.param(['ru-1/srv-1'], True, id='matches_scoped_name'),
        pytest.param(['ru-2/srv-1'], False, id='scope_to_other_region_misses'),
    ],
)
def test_is_ignored(entries, expected):
    server = {'id': 'id-1', 'name': 'srv-1', 'region': 'ru-1'}

    ignored = main.parse_ignore_list(entries)

    assert main.is_ignored(server, ignored) is expected


@pytest.mark.parametrize(
    'servers, ignored, start_side_effect, expected_start_calls, expect_sleep',
    [
        pytest.param(
            [
                {
                    'id': '1', 'name': 'a', 'status': 'ACTIVE',
                    'region': 'ru-1',
                },
                {
                    'id': '2', 'name': 'b', 'status': 'SHELVED_OFFLOADED',
                    'region': 'ru-1',
                },
            ],
            [],
            None,
            [('ru-1', '2')],
            True,
            id='starts_only_frozen',
        ),
        pytest.param(
            [{'id': '1', 'name': 'a', 'status': 'ACTIVE', 'region': 'ru-1'}],
            [],
            None,
            [],
            False,
            id='no_frozen_servers_skips_sleep',
        ),
        pytest.param(
            [
                {
                    'id': '1', 'name': 'a', 'status': 'SHELVED_OFFLOADED',
                    'region': 'ru-1',
                },
                {
                    'id': '2', 'name': 'b', 'status': 'SHELVED_OFFLOADED',
                    'region': 'ru-2',
                },
            ],
            [],
            [Exception('boom'), None],
            [('ru-1', '1'), ('ru-2', '2')],
            True,
            id='continues_after_one_failure',
        ),
        pytest.param(
            [
                {
                    'id': '1', 'name': 'a', 'status': 'SHELVED_OFFLOADED',
                    'region': 'ru-1',
                },
                {
                    'id': '2', 'name': 'b', 'status': 'SHELVED_OFFLOADED',
                    'region': 'ru-2',
                },
            ],
            ['a'],
            None,
            [('ru-2', '2')],
            True,
            id='skips_ignored_server_but_starts_others',
        ),
        pytest.param(
            [
                {
                    'id': '1', 'name': 'a', 'status': 'SHELVED_OFFLOADED',
                    'region': 'ru-1',
                },
            ],
            ['a'],
            None,
            [],
            False,
            id='ignoring_every_frozen_server_skips_sleep',
        ),
    ],
)
def test_start_frozen_servers(
    monkeypatch, logger, servers, ignored, start_side_effect,
    expected_start_calls, expect_sleep,
):
    client = Mock()
    if start_side_effect is not None:
        client.start_server.side_effect = start_side_effect
    sleep_mock = Mock()
    monkeypatch.setattr(main.time, 'sleep', sleep_mock)

    main.start_frozen_servers(
        client, servers, main.parse_ignore_list(ignored), logger,
    )

    actual_calls = [call.args for call in client.start_server.call_args_list]
    assert actual_calls == expected_start_calls
    assert sleep_mock.called is expect_sleep


def test_export_metrics_sets_values_and_clears_stale_labels(
    gauge, ignored_gauge, registry,
):
    main.export_metrics(gauge, ignored_gauge, [
        {'id': 'a1', 'name': 'srv-1', 'region': 'ru-1', 'status': 'ACTIVE'},
        {
            'id': 'a2', 'name': 'srv-2', 'region': 'ru-1',
            'status': 'SHELVED_OFFLOADED',
        },
    ], main.parse_ignore_list([]))

    assert registry.get_sample_value(
        'selectel_server_up',
        {'server_name': 'srv-1', 'server_id': 'a1', 'region': 'ru-1'},
    ) == 1
    assert registry.get_sample_value(
        'selectel_server_up',
        {'server_name': 'srv-2', 'server_id': 'a2', 'region': 'ru-1'},
    ) == 0

    main.export_metrics(
        gauge, ignored_gauge,
        [{
            'id': 'a3', 'name': 'srv-3', 'region': 'ru-1',
            'status': 'ACTIVE',
        }],
        main.parse_ignore_list([]),
    )

    assert registry.get_sample_value(
        'selectel_server_up',
        {'server_name': 'srv-1', 'server_id': 'a1', 'region': 'ru-1'},
    ) is None
    assert registry.get_sample_value(
        'selectel_server_up',
        {'server_name': 'srv-2', 'server_id': 'a2', 'region': 'ru-1'},
    ) is None
    assert registry.get_sample_value(
        'selectel_server_up',
        {'server_name': 'srv-3', 'server_id': 'a3', 'region': 'ru-1'},
    ) == 1


def test_export_metrics_flags_ignored_servers(gauge, ignored_gauge, registry):
    main.export_metrics(
        gauge, ignored_gauge,
        [
            {
                'id': '1', 'name': 'srv-1', 'region': 'ru-1',
                'status': 'ACTIVE',
            },
            {
                'id': '2', 'name': 'srv-2', 'region': 'ru-1',
                'status': 'SHELVED_OFFLOADED',
            },
        ],
        main.parse_ignore_list(['srv-2']),
    )

    assert registry.get_sample_value(
        'selectel_server_ignored',
        {'server_name': 'srv-1', 'server_id': '1', 'region': 'ru-1'},
    ) == 0
    assert registry.get_sample_value(
        'selectel_server_ignored',
        {'server_name': 'srv-2', 'server_id': '2', 'region': 'ru-1'},
    ) == 1

    # An ignored server still reports its real status.
    assert registry.get_sample_value(
        'selectel_server_up',
        {'server_name': 'srv-2', 'server_id': '2', 'region': 'ru-1'},
    ) == 0
