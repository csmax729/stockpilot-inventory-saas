"""Malformed cursors must be client errors, without needing a database."""
import base64
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app import main


def encoded(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()


@pytest.fixture
def client(monkeypatch):
    main.app.dependency_overrides[main.db_session] = lambda: object()
    main.app.dependency_overrides[main.current_user] = lambda: {'id': uuid4()}
    monkeypatch.setattr(main, 'member', lambda *args: 'admin')
    with TestClient(main.app) as test_client:
        yield test_client
    main.app.dependency_overrides.clear()


@pytest.mark.parametrize('cursor', [
    '',  # an explicitly supplied empty cursor is not an omitted cursor
    'a',  # invalid base64 padding
    'abcd$',  # invalid alphabet, previously ignored by the decoder
    'é',  # not ASCII base64
    encoded({'value': 'not a list'}),
    encoded(['only one element']),
    encoded([1, 2]),  # must not reach UUID parsing as non-strings
    encoded([None, None]),
    encoded(['not a timestamp or UUID', 'also invalid']),
    'x' * 1025,
])
@pytest.mark.parametrize('resource', ['products', 'inventory'])
def test_malformed_cursor_returns_400(client, resource, cursor):
    response = client.get(f'/api/v1/organizations/{uuid4()}/{resource}', params={'cursor': cursor})
    assert response.status_code == 400
    assert response.json()['error']['code'] == 'INVALID_CURSOR'


def test_cursor_round_trip():
    values = ['2026-11-13T12:00:00+00:00', str(uuid4())]
    assert main.parse_cursor(main.page_cursor(values), 2) == values
    assert main.parse_cursor(None, 2) is None
