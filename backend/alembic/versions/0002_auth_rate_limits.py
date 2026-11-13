"""Persist authentication rate limits across API workers."""
from alembic import op

revision = '0002_auth_rate_limits'
down_revision = '0001_baseline'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE auth_rate_limits (
        scope TEXT NOT NULL,
        key_hash CHAR(64) NOT NULL,
        window_start TIMESTAMPTZ NOT NULL,
        attempts INTEGER NOT NULL CHECK (attempts > 0),
        PRIMARY KEY (scope, key_hash)
    )''')
    op.execute('CREATE INDEX idx_auth_rate_limits_window ON auth_rate_limits (window_start)')


def downgrade():
    op.execute('DROP TABLE auth_rate_limits')
