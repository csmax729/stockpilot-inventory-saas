"""Apply the reviewed PostgreSQL baseline."""
from pathlib import Path
from alembic import op

revision = '0001_baseline'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    sql = (Path(__file__).resolve().parents[2] / 'schema.sql').read_text(encoding='utf-8')
    # Alembic owns the transaction, not the reference bootstrap file.
    sql = sql.replace('BEGIN;', '', 1).rsplit('COMMIT;', 1)[0]
    op.get_bind().exec_driver_sql(sql)

def downgrade():
    op.execute('DROP TABLE audit_logs, auth_sessions, idempotency_records, stock_movements, order_items, orders, inventory, products, warehouses, memberships, organizations, users CASCADE')
    op.execute('DROP FUNCTION touch_updated_at()')
