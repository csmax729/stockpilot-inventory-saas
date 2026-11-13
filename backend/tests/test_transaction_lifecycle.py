"""Prove commit-time failures are reported before an HTTP success is sent.

Run with TEST_DATABASE_URL pointing to a separate, migrated PostgreSQL database.
"""
import os
from uuid import uuid4

import pytest


@pytest.fixture
def application():
    url = os.environ.get('TEST_DATABASE_URL')
    if url and not url.startswith(('postgresql://', 'postgresql+psycopg://')):
        pytest.fail('Integration tests require PostgreSQL')
    if url:
        os.environ['DATABASE_URL'] = url.replace('postgresql://', 'postgresql+psycopg://', 1)
    from app import main
    return main


def test_commit_failure_is_reported_before_response(application, monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy.exc import IntegrityError

    class FailingSession:
        rolled_back = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args):
            return None

        def commit(self):
            raise IntegrityError('COMMIT', {}, Exception('simulated commit failure'))

        def rollback(self):
            self.rolled_back = True

    session = FailingSession()
    monkeypatch.setattr(application, 'SessionLocal', lambda: session)
    monkeypatch.setattr(application, 'one', lambda *args, **kwargs: {'id': uuid4(), 'name': 'Probe'})
    monkeypatch.setattr(application, 'audit', lambda *args, **kwargs: None)
    application.app.dependency_overrides[application.current_user] = lambda: {'id': uuid4()}
    try:
        with TestClient(application.app, raise_server_exceptions=False) as client:
            response = client.post('/api/v1/organizations', json={
                'name': 'Probe', 'slug': 'commit-probe', 'default_currency': 'INR'
            })
    finally:
        application.app.dependency_overrides.pop(application.current_user, None)

    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'CONFLICT'
    assert session.rolled_back


def test_commit_failure_returns_error_and_rolls_back(application):
    from fastapi.testclient import TestClient
    from sqlalchemy import event, text

    if not os.environ.get('TEST_DATABASE_URL'):
        pytest.skip('Set TEST_DATABASE_URL to an isolated, migrated PostgreSQL database')
    credentials = {'email': f'commit-{uuid4().hex}@example.com', 'password': 'test-password-1234'}
    slug = f'commit-{uuid4().hex}'
    with TestClient(application.app, raise_server_exceptions=False) as client:
        assert client.post('/api/v1/auth/register', json=credentials).status_code == 201
        login = client.post('/api/v1/auth/login', json=credentials)
        assert login.status_code == 200
        headers = {'Authorization': f"Bearer {login.json()['access_token']}"}
        called = []

        def add_deferred_violation(session):
            # A deferred unique constraint fails at COMMIT, not at INSERT. The
            # temporary table and failed mutation roll back with the request.
            called.append(True)
            session.execute(text('CREATE TEMP TABLE commit_failure_probe (value integer UNIQUE DEFERRABLE INITIALLY DEFERRED) ON COMMIT DROP'))
            session.execute(text('INSERT INTO commit_failure_probe (value) VALUES (1), (1)'))

        event.listen(application.SessionLocal, 'before_commit', add_deferred_violation)
        try:
            response = client.post('/api/v1/organizations', headers=headers, json={
                'name': 'Commit failure probe', 'slug': slug, 'default_currency': 'INR'
            })
        finally:
            event.remove(application.SessionLocal, 'before_commit', add_deferred_violation)

    assert called == [True]
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'CONFLICT'
    with application.engine.connect() as connection:
        assert connection.execute(text('SELECT count(*) FROM organizations WHERE slug=:slug'), {'slug': slug}).scalar_one() == 0
