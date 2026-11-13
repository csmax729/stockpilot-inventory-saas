"""Configure the isolated database before any test module imports the app."""
import os


test_url = os.environ.get('TEST_DATABASE_URL')
if test_url:
    os.environ['DATABASE_URL'] = test_url.replace('postgresql://', 'postgresql+psycopg://', 1)
