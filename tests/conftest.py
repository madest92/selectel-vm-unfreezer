import logging

import pytest
from prometheus_client import CollectorRegistry

import main


@pytest.fixture
def logger():
    return logging.getLogger('selectel-vm-unfreezer-tests')


@pytest.fixture
def client(logger):
    client = main.SelectelClient(
        account='acct', project='proj', user='usr', password='pwd',
        logger=logger, regions=['ru-1', 'ru-2'],
    )
    client._endpoints = {
        'ru-1': 'https://ru-1.cloud.api.selcloud.ru/compute/v2.1',
        'ru-2': 'https://ru-2.cloud.api.selcloud.ru/compute/v2.1',
    }
    return client


@pytest.fixture
def registry():
    return CollectorRegistry()


@pytest.fixture
def metrics(registry):
    return main.Metrics(registry)
