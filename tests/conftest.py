import logging

import pytest
from prometheus_client import CollectorRegistry, Gauge

import main


@pytest.fixture
def logger():
    return logging.getLogger('selectel-vm-unfreezer-tests')


@pytest.fixture
def client(logger):
    return main.SelectelClient(
        account='acct', project='proj', user='usr', password='pwd',
        regions=['ru-1', 'ru-2'], logger=logger,
    )


@pytest.fixture
def registry():
    return CollectorRegistry()


@pytest.fixture
def gauge(registry):
    return Gauge(
        'selectel_server_up', 'test gauge',
        ['server_name', 'server_id', 'region'],
        registry=registry,
    )


@pytest.fixture
def ignored_gauge(registry):
    return Gauge(
        'selectel_server_ignored', 'test gauge',
        ['server_name', 'server_id', 'region'],
        registry=registry,
    )
