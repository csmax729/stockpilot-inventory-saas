-- StockPilot PostgreSQL 16+ baseline schema (MVP)
-- Run through Alembic migrations in production; this file is a reference bootstrap schema.
-- All timestamps are UTC-aware timestamptz; quantities are whole stock units.
BEGIN;

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT NOT NULL CHECK (length(btrim(email)) > 3),
    password_hash TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_users_email_ci ON users ((lower(email)));

CREATE TABLE organizations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    slug TEXT NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    name TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
    default_currency CHAR(3) NOT NULL DEFAULT 'INR' CHECK (default_currency ~ '^[A-Z]{3}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE memberships (
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    role TEXT NOT NULL CHECK (role IN ('admin','manager','staff','viewer')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, user_id)
);
CREATE INDEX idx_memberships_user ON memberships (user_id, organization_id);

CREATE TABLE warehouses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    code TEXT NOT NULL CHECK (length(btrim(code)) BETWEEN 1 AND 40),
    name TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 120),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, code)
);

CREATE TABLE products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    sku TEXT NOT NULL CHECK (length(btrim(sku)) BETWEEN 1 AND 80),
    name TEXT NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 200),
    description TEXT,
    unit_price_minor BIGINT NOT NULL CHECK (unit_price_minor >= 0),
    low_stock_threshold INTEGER NOT NULL DEFAULT 5 CHECK (low_stock_threshold >= 0),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, sku)
);
CREATE INDEX idx_products_tenant_active_name ON products (organization_id, is_active, name);

-- available_qty is DERIVED: on_hand_qty - reserved_qty; never persisted separately.
CREATE TABLE inventory (
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    warehouse_id UUID NOT NULL,
    product_id UUID NOT NULL,
    on_hand_qty INTEGER NOT NULL DEFAULT 0 CHECK (on_hand_qty >= 0),
    reserved_qty INTEGER NOT NULL DEFAULT 0 CHECK (reserved_qty >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (organization_id, warehouse_id, product_id),
    FOREIGN KEY (organization_id, warehouse_id)
        REFERENCES warehouses (organization_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (organization_id, product_id)
        REFERENCES products (organization_id, id) ON DELETE RESTRICT,
    CHECK (reserved_qty <= on_hand_qty)
);
CREATE INDEX idx_inventory_tenant_product ON inventory (organization_id, product_id);

CREATE TABLE orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    warehouse_id UUID NOT NULL,
    created_by UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    order_number TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved','fulfilled','cancelled')),
    customer_name TEXT NOT NULL CHECK (length(btrim(customer_name)) BETWEEN 1 AND 160),
    customer_email TEXT,
    currency CHAR(3) NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    total_minor BIGINT NOT NULL CHECK (total_minor >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    fulfilled_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    UNIQUE (organization_id, id),
    UNIQUE (organization_id, order_number),
    FOREIGN KEY (organization_id, warehouse_id)
        REFERENCES warehouses (organization_id, id) ON DELETE RESTRICT,
    CHECK (
       (status = 'reserved' AND fulfilled_at IS NULL AND cancelled_at IS NULL) OR
       (status = 'fulfilled' AND fulfilled_at IS NOT NULL AND cancelled_at IS NULL) OR
       (status = 'cancelled' AND fulfilled_at IS NULL AND cancelled_at IS NOT NULL)
    )
);
CREATE INDEX idx_orders_tenant_created ON orders (organization_id, created_at DESC, id DESC);
CREATE INDEX idx_orders_tenant_status_created ON orders (organization_id, status, created_at DESC);
CREATE INDEX idx_orders_tenant_warehouse ON orders (organization_id, warehouse_id, created_at DESC);

CREATE TABLE order_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    order_id UUID NOT NULL,
    product_id UUID NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_minor BIGINT NOT NULL CHECK (unit_price_minor >= 0),
    line_total_minor BIGINT NOT NULL
        CHECK (line_total_minor = quantity::bigint * unit_price_minor),
    UNIQUE (organization_id, order_id, product_id),
    FOREIGN KEY (organization_id, order_id)
        REFERENCES orders (organization_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (organization_id, product_id)
        REFERENCES products (organization_id, id) ON DELETE RESTRICT
);

CREATE TABLE stock_movements (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    warehouse_id UUID NOT NULL,
    product_id UUID NOT NULL,
    order_id UUID,
    movement_type TEXT NOT NULL CHECK (
        movement_type IN ('receipt','adjustment','reservation','release','fulfillment')
    ),
    delta_on_hand INTEGER NOT NULL DEFAULT 0,
    delta_reserved INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    actor_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (organization_id, warehouse_id, product_id)
        REFERENCES inventory (organization_id, warehouse_id, product_id) ON DELETE RESTRICT,
    FOREIGN KEY (organization_id, order_id)
        REFERENCES orders (organization_id, id) ON DELETE RESTRICT,
    CHECK (delta_on_hand <> 0 OR delta_reserved <> 0)
);
CREATE INDEX idx_movements_tenant_time ON stock_movements (organization_id, created_at DESC);
CREATE INDEX idx_movements_tenant_order ON stock_movements (organization_id, order_id);

-- One logical order-create side effect per tenant, actor, route, key during retention.
CREATE TABLE idempotency_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    actor_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    method TEXT NOT NULL,
    route TEXT NOT NULL,
    key TEXT NOT NULL CHECK (length(key) BETWEEN 8 AND 128),
    request_hash CHAR(64) NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','completed')),
    response_code INTEGER,
    response_body JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    UNIQUE (organization_id, actor_user_id, method, route, key),
    CHECK (
        (state = 'pending' AND response_code IS NULL AND response_body IS NULL) OR
        (state = 'completed' AND response_code IS NOT NULL AND response_body IS NOT NULL)
    ),
    CHECK (expires_at > created_at)
);
CREATE INDEX idx_idempotency_expiry ON idempotency_records (expires_at);

-- Refresh tokens are random opaque values; only their SHA-256 hashes are persisted.
CREATE TABLE auth_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    family_id UUID NOT NULL,
    refresh_token_hash CHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    rotated_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    CHECK (expires_at > created_at)
);
CREATE INDEX idx_auth_sessions_user ON auth_sessions (user_id, expires_at DESC);
CREATE INDEX idx_auth_sessions_family ON auth_sessions (family_id);

CREATE TABLE audit_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id UUID NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    actor_user_id UUID NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id UUID NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_audit_tenant_created ON audit_logs (organization_id, created_at DESC);

CREATE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER users_touch BEFORE UPDATE ON users
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER orgs_touch BEFORE UPDATE ON organizations
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER warehouses_touch BEFORE UPDATE ON warehouses
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER products_touch BEFORE UPDATE ON products
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER inventory_touch BEFORE UPDATE ON inventory
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
CREATE TRIGGER orders_touch BEFORE UPDATE ON orders
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

COMMIT;
