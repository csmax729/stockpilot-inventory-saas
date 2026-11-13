"""StockPilot's tenant-scoped, transactional API."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import UUID

import jwt
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, Security
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWTError
from pwdlib import PasswordHash
from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql+psycopg://stockpilot:stockpilot@localhost:5432/stockpilot')
SECRET_KEY = os.getenv('SECRET_KEY', 'local-development-only-change-this-secret')
ORIGIN = os.getenv('FRONTEND_ORIGIN', 'http://localhost:5173')
COOKIE_SECURE = os.getenv('COOKIE_SECURE', 'false').lower() == 'true'
APP_ENV = os.getenv('APP_ENV', 'development').lower()
if APP_ENV not in {'development', 'test', 'production'}:
    raise RuntimeError('APP_ENV must be development, test, or production')
if APP_ENV == 'production':
    if len(SECRET_KEY) < 32 or SECRET_KEY in {'local-development-only-change-this-secret', 'replace-this-with-a-long-random-secret-before-deployment'}:
        raise RuntimeError('Production requires a unique SECRET_KEY of at least 32 characters')
    if not COOKIE_SECURE or not ORIGIN.startswith('https://'):
        raise RuntimeError('Production requires COOKIE_SECURE=true and an HTTPS FRONTEND_ORIGIN')
JWT_ISSUER = os.getenv('JWT_ISSUER', 'stockpilot')
JWT_AUDIENCE = os.getenv('JWT_AUDIENCE', 'stockpilot-api')
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)
passwords = PasswordHash.recommended()
dummy_password_hash = passwords.hash(secrets.token_urlsafe(32))
app = FastAPI(title='StockPilot API', version='1.0.0')
app.add_middleware(CORSMiddleware, allow_origins=[ORIGIN], allow_credentials=True,
                   allow_methods=['GET', 'POST', 'PATCH'], allow_headers=['Authorization', 'Content-Type', 'Idempotency-Key', 'X-CSRF-Token'])


@app.middleware('http')
async def request_id(request: Request, call_next):
    request.state.request_id = str(uuid.uuid4())
    response = await call_next(request)
    response.headers['X-Request-ID'] = request.state.request_id
    return response


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {'code': 'ERROR', 'message': str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={'error': {**detail, 'request_id': request.state.request_id}}, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # Never echo submitted values or exception context: they can contain credentials.
    details = [{key: error[key] for key in ('type', 'loc', 'msg') if key in error} for error in exc.errors()]
    return JSONResponse(status_code=422, content={'error': {'code': 'VALIDATION_ERROR', 'message': 'Invalid request', 'request_id': request.state.request_id, 'details': jsonable_encoder(details)}})


@app.exception_handler(IntegrityError)
async def integrity_error(request: Request, exc: IntegrityError):
    return JSONResponse(status_code=409, content={'error': {'code': 'CONFLICT', 'message': 'A record with these values already exists or violates a constraint', 'request_id': request.state.request_id}})


def fail(status: int, code: str, message: str):
    raise HTTPException(status, detail={'code': code, 'message': message})


def db_session():
    with SessionLocal() as db:
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise

DB = Annotated[Session, Depends(db_session)]
bearer_auth = HTTPBearer(auto_error=False)


def one(db, sql, **values):
    row = db.execute(text(sql), values).mappings().first()
    return dict(row) if row else None


def many(db, sql, **values):
    return [dict(row) for row in db.execute(text(sql), values).mappings()]


def current_user(db: DB, authorization: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_auth)]):
    if not authorization or authorization.scheme.lower() != 'bearer':
        fail(401, 'UNAUTHORIZED', 'Authentication required')
    try:
        claims = jwt.decode(authorization.credentials, SECRET_KEY, algorithms=['HS256'], issuer=JWT_ISSUER, audience=JWT_AUDIENCE, options={'require': ['sub', 'jti', 'iss', 'aud', 'iat', 'nbf', 'exp']})
        user_id = UUID(claims['sub'])
    except (PyJWTError, ValueError, KeyError):
        fail(401, 'UNAUTHORIZED', 'Invalid or expired access token')
    user = one(db, 'SELECT id, email, is_active FROM users WHERE id=:id AND is_active', id=user_id)
    if not user:
        fail(401, 'UNAUTHORIZED', 'Account unavailable')
    return user

User = Annotated[dict, Depends(current_user)]


def member(db, org_id, user, roles=None):
    membership = one(db, 'SELECT role FROM memberships WHERE organization_id=:org AND user_id=:user', org=org_id, user=user['id'])
    if not membership:
        fail(404, 'NOT_FOUND', 'Organization not found')
    if roles and membership['role'] not in roles:
        fail(403, 'FORBIDDEN', 'Insufficient permissions')
    return membership['role']


def audit(db, org, user, action, entity_type, entity_id, details=None):
    db.execute(text('INSERT INTO audit_logs (organization_id,actor_user_id,action,entity_type,entity_id,details) VALUES (:org,:user,:action,:type,:id,CAST(:details AS jsonb))'),
               dict(org=org, user=user['id'], action=action, type=entity_type, id=entity_id, details=json.dumps(details or {})))


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

class Register(Input):
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)

class Login(Input):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

class OrganizationInput(Input):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(pattern=r'^[a-z0-9]+(-[a-z0-9]+)*$', max_length=120)
    default_currency: str = Field(default='INR', pattern=r'^[A-Z]{3}$')

Role = Literal['admin', 'manager', 'staff', 'viewer']
class MemberInput(Input):
    email: EmailStr
    role: Role
class RoleInput(Input):
    role: Role
class ProductInput(Input):
    sku: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    unit_price_minor: int = Field(ge=0, le=9223372036854775807)
    low_stock_threshold: int = Field(default=5, ge=0, le=2147483647)
class ProductPatch(Input):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    unit_price_minor: int | None = Field(default=None, ge=0, le=9223372036854775807)
    low_stock_threshold: int | None = Field(default=None, ge=0, le=2147483647)
    is_active: bool | None = None
class WarehouseInput(Input):
    code: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
class WarehousePatch(Input):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    is_active: bool | None = None
class AdjustmentInput(Input):
    warehouse_id: UUID
    product_id: UUID
    delta_on_hand: int = Field(ge=-2147483647, le=2147483647)
    reason: str = Field(min_length=3, max_length=500)
    @model_validator(mode='after')
    def nonzero(self):
        if self.delta_on_hand == 0:
            raise ValueError('Adjustment must not be zero')
        return self
class LineInput(Input):
    product_id: UUID
    quantity: int = Field(ge=1, le=10000)
class OrderInput(Input):
    warehouse_id: UUID
    customer_name: str = Field(min_length=1, max_length=160)
    customer_email: EmailStr | None = None
    items: list[LineInput] = Field(min_length=1, max_length=50)
    @model_validator(mode='after')
    def unique_products(self):
        if len({line.product_id for line in self.items}) != len(self.items):
            raise ValueError('Duplicate products are not allowed')
        return self


def access(user_id):
    now = datetime.now(timezone.utc)
    return {'access_token': jwt.encode({'sub': str(user_id), 'jti': str(uuid.uuid4()), 'iss': JWT_ISSUER, 'aud': JWT_AUDIENCE, 'iat': now, 'nbf': now, 'exp': now + timedelta(minutes=15)}, SECRET_KEY, algorithm='HS256'), 'token_type': 'bearer', 'expires_in': 900}

def set_session(db, response, user_id, family_id=None):
    token = secrets.token_urlsafe(48)
    family = family_id or uuid.uuid4()
    db.execute(text('INSERT INTO auth_sessions (user_id,family_id,refresh_token_hash,expires_at) VALUES (:user,:family,:hash,now()+interval \'14 days\')'),
               dict(user=user_id, family=family, hash=hashlib.sha256(token.encode()).hexdigest()))
    nonce = secrets.token_urlsafe(24)
    signature = hmac.new(SECRET_KEY.encode(), (token + ':' + nonce).encode(), hashlib.sha256).hexdigest()
    response.set_cookie('sp_refresh', token, max_age=1209600, httponly=True, secure=COOKIE_SECURE, samesite='lax', path='/api/v1/auth')
    response.set_cookie('sp_csrf', nonce + '.' + signature, max_age=1209600, httponly=False, secure=COOKIE_SECURE, samesite='lax', path='/')

def rate_limit(scope: str, identity: str, limit: int):
    """Shared fixed one-minute window, persisted separately from request rollback."""
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()
    with SessionLocal.begin() as session:
        # Bound storage from old identities without a separate maintenance job.
        if secrets.randbelow(100) == 0:
            session.execute(text("DELETE FROM auth_rate_limits WHERE window_start < now() - interval '1 day'"))
        row = one(session, '''
            INSERT INTO auth_rate_limits (scope, key_hash, window_start, attempts)
            VALUES (:scope, :key, now(), 1)
            ON CONFLICT (scope, key_hash) DO UPDATE SET
              attempts = CASE WHEN auth_rate_limits.window_start <= now() - interval '1 minute'
                              THEN 1 ELSE auth_rate_limits.attempts + 1 END,
              window_start = CASE WHEN auth_rate_limits.window_start <= now() - interval '1 minute'
                                  THEN now() ELSE auth_rate_limits.window_start END
            RETURNING attempts
        ''', scope=scope, key=digest)
    if row['attempts'] > limit:
        raise HTTPException(429, detail={'code': 'RATE_LIMITED', 'message': 'Too many requests; try again later'}, headers={'Retry-After': '60'})

def auth_rate_limit(request: Request, action: str, email: str):
    # Use the actual peer, never an untrusted X-Forwarded-For header. A reverse
    # proxy must supply the real client address through trusted server settings.
    peer = request.client.host if request.client else 'unknown'
    rate_limit(action + ':ip', peer, 100 if action == 'login' else 30)
    if action == 'login':
        rate_limit(action + ':identity', email.casefold(), 5)

def check_csrf(request):
    token = request.cookies.get('sp_refresh', '')
    csrf = request.cookies.get('sp_csrf', '')
    supplied = request.headers.get('X-CSRF-Token', '')
    origin = request.headers.get('Origin')
    parts = csrf.split('.')
    valid = len(parts) == 2 and hmac.compare_digest(parts[1], hmac.new(SECRET_KEY.encode(), (token + ':' + parts[0]).encode(), hashlib.sha256).hexdigest())
    if origin != ORIGIN or not supplied or not hmac.compare_digest(csrf, supplied) or not valid:
        fail(403, 'CSRF_INVALID', 'Invalid origin or CSRF token')
    return token

@app.get('/health/live')
def live():
    return {'status': 'alive'}

@app.get('/health/ready')
def ready():
    try:
        with engine.connect() as connection:
            connection.execute(text('SELECT 1'))
        return {'status': 'ready'}
    except Exception:
        fail(503, 'UNAVAILABLE', 'Database unavailable')

@app.post('/api/v1/auth/register', status_code=201)
def register(data: Register, request: Request, db: DB):
    auth_rate_limit(request, 'register', data.email)
    return one(db, 'INSERT INTO users (email,password_hash) VALUES (:email,:password) RETURNING id,email,is_active', email=data.email.lower(), password=passwords.hash(data.password))

@app.post('/api/v1/auth/login')
def login(data: Login, request: Request, response: Response, db: DB):
    auth_rate_limit(request, 'login', data.email)
    user = one(db, 'SELECT id,password_hash,is_active FROM users WHERE lower(email)=lower(:email)', email=data.email)
    verified = passwords.verify(data.password, user['password_hash'] if user else dummy_password_hash)
    if not user or not user['is_active'] or not verified:
        fail(401, 'UNAUTHORIZED', 'Invalid credentials')
    set_session(db, response, user['id'])
    return access(user['id'])

@app.post('/api/v1/auth/refresh')
def refresh(request: Request, response: Response, db: DB):
    token = check_csrf(request)
    session = one(db, 'SELECT s.*,u.is_active FROM auth_sessions s JOIN users u ON u.id=s.user_id WHERE refresh_token_hash=:hash FOR UPDATE OF s', hash=hashlib.sha256(token.encode()).hexdigest())
    if not session:
        fail(401, 'UNAUTHORIZED', 'Invalid refresh token')
    if session['rotated_at'] or session['revoked_at']:
        db.execute(text('UPDATE auth_sessions SET revoked_at=now() WHERE family_id=:family AND revoked_at IS NULL'), {'family': session['family_id']})
        db.commit()  # Persist reuse detection despite the error response.
        fail(401, 'UNAUTHORIZED', 'Refresh token reuse detected')
    if not session['is_active'] or session['expires_at'] <= datetime.now(timezone.utc):
        fail(401, 'UNAUTHORIZED', 'Expired refresh token')
    db.execute(text('UPDATE auth_sessions SET rotated_at=now() WHERE id=:id'), {'id': session['id']})
    set_session(db, response, session['user_id'], session['family_id'])
    return access(session['user_id'])

@app.post('/api/v1/auth/logout', status_code=204)
def logout(request: Request, response: Response, db: DB):
    token = check_csrf(request)
    db.execute(text('UPDATE auth_sessions SET revoked_at=now() WHERE family_id=(SELECT family_id FROM auth_sessions WHERE refresh_token_hash=:hash) AND revoked_at IS NULL'), {'hash': hashlib.sha256(token.encode()).hexdigest()})
    response.delete_cookie('sp_refresh', path='/api/v1/auth')
    response.delete_cookie('sp_csrf', path='/')

@app.get('/api/v1/auth/me')
def me(user: User):
    return user

@app.get('/api/v1/organizations')
def organizations(db: DB, user: User):
    return many(db, 'SELECT o.*,m.role FROM organizations o JOIN memberships m ON m.organization_id=o.id WHERE m.user_id=:user ORDER BY o.created_at,o.id', user=user['id'])

@app.post('/api/v1/organizations', status_code=201)
def create_organization(data: OrganizationInput, db: DB, user: User):
    org = one(db, 'INSERT INTO organizations (name,slug,default_currency) VALUES (:name,:slug,:default_currency) RETURNING *', **data.model_dump())
    db.execute(text("INSERT INTO memberships (organization_id,user_id,role) VALUES (:org,:user,'admin')"), {'org': org['id'], 'user': user['id']})
    audit(db, org['id'], user, 'organization.created', 'organization', org['id'])
    org['role'] = 'admin'
    return org

@app.get('/api/v1/organizations/{org_id}')
def get_organization(org_id: UUID, db: DB, user: User):
    role = member(db, org_id, user)
    org = one(db, 'SELECT * FROM organizations WHERE id=:org', org=org_id)
    org['role'] = role
    return org

@app.get('/api/v1/organizations/{org_id}/members')
def members(org_id: UUID, db: DB, user: User):
    member(db, org_id, user, ['admin', 'manager'])
    return many(db, 'SELECT m.organization_id,m.user_id,u.email,m.role FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.organization_id=:org ORDER BY u.email', org=org_id)

@app.post('/api/v1/organizations/{org_id}/members', status_code=201)
def add_member(org_id: UUID, data: MemberInput, db: DB, user: User):
    member(db, org_id, user, ['admin'])
    target = one(db, 'SELECT id,email FROM users WHERE lower(email)=lower(:email) AND is_active', email=data.email)
    if not target:
        fail(404, 'NOT_FOUND', 'Registered user not found')
    db.execute(text('INSERT INTO memberships (organization_id,user_id,role) VALUES (:org,:user,:role)'), {'org': org_id, 'user': target['id'], 'role': data.role})
    audit(db, org_id, user, 'member.added', 'membership', target['id'], {'role': data.role})
    return {'organization_id': org_id, 'user_id': target['id'], 'email': target['email'], 'role': data.role}

@app.patch('/api/v1/organizations/{org_id}/members/{user_id}')
def change_member(org_id: UUID, user_id: UUID, data: RoleInput, db: DB, user: User):
    member(db, org_id, user, ['admin'])
    # Serialize admin changes for the organization so concurrent demotions cannot remove the last admin.
    db.execute(text('SELECT id FROM organizations WHERE id=:org FOR UPDATE'), {'org': org_id})
    member(db, org_id, user, ['admin'])
    target = one(db, 'SELECT m.role,u.email FROM memberships m JOIN users u ON u.id=m.user_id WHERE m.organization_id=:org AND m.user_id=:user', org=org_id, user=user_id)
    if not target:
        fail(404, 'NOT_FOUND', 'Member not found')
    if target['role'] == 'admin' and data.role != 'admin':
        count = one(db, "SELECT count(*) AS n FROM memberships WHERE organization_id=:org AND role='admin'", org=org_id)['n']
        if count <= 1:
            fail(409, 'LAST_ADMIN', 'Organization must retain an admin')
    db.execute(text('UPDATE memberships SET role=:role WHERE organization_id=:org AND user_id=:user'), {'role': data.role, 'org': org_id, 'user': user_id})
    audit(db, org_id, user, 'member.role_changed', 'membership', user_id, {'role': data.role})
    return {'organization_id': org_id, 'user_id': user_id, 'email': target['email'], 'role': data.role}


def page_cursor(values):
    return base64.urlsafe_b64encode(json.dumps([str(value) for value in values]).encode()).decode()

def parse_cursor(cursor, size):
    if not cursor:
        return None
    try:
        values = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        if not isinstance(values, list) or len(values) != size:
            raise ValueError()
        return values
    except (ValueError, TypeError, UnicodeDecodeError):
        fail(400, 'INVALID_CURSOR', 'Invalid pagination cursor')

def time_page(db, table, org_id, limit, cursor, extra='', **params):
    values = parse_cursor(cursor, 2)
    if values:
        try:
            params.update(cursor_time=datetime.fromisoformat(values[0]), cursor_id=UUID(values[1]))
        except (ValueError, TypeError):
            fail(400, 'INVALID_CURSOR', 'Invalid pagination cursor')
    where = ' AND (created_at,id) < (:cursor_time,:cursor_id)' if values else ''
    rows = many(db, f'SELECT * FROM {table} WHERE organization_id=:org {extra} {where} ORDER BY created_at DESC,id DESC LIMIT :limit', org=org_id, limit=limit + 1, **params)
    next_cursor = page_cursor([rows[limit - 1]['created_at'], rows[limit - 1]['id']]) if len(rows) > limit else None
    return {'items': rows[:limit], 'next_cursor': next_cursor}

@app.get('/api/v1/organizations/{org_id}/products')
def products(org_id: UUID, db: DB, user: User, limit: int = Query(25, ge=1, le=100), cursor: str | None = None, q: str | None = Query(None, max_length=100), active: bool | None = None):
    member(db, org_id, user)
    extra, params = '', {}
    if q:
        extra += ' AND (name ILIKE :q OR sku ILIKE :q)'
        params['q'] = '%' + q + '%'
    if active is not None:
        extra += ' AND is_active=:active'
        params['active'] = active
    return time_page(db, 'products', org_id, limit, cursor, extra, **params)

@app.post('/api/v1/organizations/{org_id}/products', status_code=201)
def create_product(org_id: UUID, data: ProductInput, db: DB, user: User):
    member(db, org_id, user, ['admin', 'manager'])
    result = one(db, 'INSERT INTO products (organization_id,sku,name,description,unit_price_minor,low_stock_threshold) VALUES (:org,:sku,:name,:description,:unit_price_minor,:low_stock_threshold) RETURNING *', org=org_id, **data.model_dump())
    audit(db, org_id, user, 'product.created', 'product', result['id'])
    return result

@app.get('/api/v1/organizations/{org_id}/products/{product_id}')
def product(org_id: UUID, product_id: UUID, db: DB, user: User):
    member(db, org_id, user)
    result = one(db, 'SELECT * FROM products WHERE organization_id=:org AND id=:id', org=org_id, id=product_id)
    if not result:
        fail(404, 'NOT_FOUND', 'Product not found')
    return result

def patch_resource(db, table, org_id, resource_id, data):
    fields = data.model_dump(exclude_unset=True)
    if any(value is None for key, value in fields.items() if key != 'description'):
        fail(422, 'VALIDATION_ERROR', 'Fields cannot be null')
    if fields:
        assignments = ','.join(f'{key}=:{key}' for key in fields)
        row = one(db, f'UPDATE {table} SET {assignments} WHERE organization_id=:org AND id=:id RETURNING *', org=org_id, id=resource_id, **fields)
    else:
        row = one(db, f'SELECT * FROM {table} WHERE organization_id=:org AND id=:id', org=org_id, id=resource_id)
    if not row:
        fail(404, 'NOT_FOUND', 'Resource not found')
    return row

@app.patch('/api/v1/organizations/{org_id}/products/{product_id}')
def update_product(org_id: UUID, product_id: UUID, data: ProductPatch, db: DB, user: User):
    member(db, org_id, user, ['admin', 'manager'])
    result = patch_resource(db, 'products', org_id, product_id, data)
    audit(db, org_id, user, 'product.updated', 'product', product_id)
    return result

@app.get('/api/v1/organizations/{org_id}/warehouses')
def warehouses(org_id: UUID, db: DB, user: User):
    member(db, org_id, user)
    return many(db, 'SELECT * FROM warehouses WHERE organization_id=:org ORDER BY name,id', org=org_id)

@app.post('/api/v1/organizations/{org_id}/warehouses', status_code=201)
def create_warehouse(org_id: UUID, data: WarehouseInput, db: DB, user: User):
    member(db, org_id, user, ['admin', 'manager'])
    result = one(db, 'INSERT INTO warehouses (organization_id,code,name) VALUES (:org,:code,:name) RETURNING *', org=org_id, **data.model_dump())
    audit(db, org_id, user, 'warehouse.created', 'warehouse', result['id'])
    return result

@app.patch('/api/v1/organizations/{org_id}/warehouses/{warehouse_id}')
def update_warehouse(org_id: UUID, warehouse_id: UUID, data: WarehousePatch, db: DB, user: User):
    member(db, org_id, user, ['admin', 'manager'])
    result = patch_resource(db, 'warehouses', org_id, warehouse_id, data)
    audit(db, org_id, user, 'warehouse.updated', 'warehouse', warehouse_id)
    return result

@app.get('/api/v1/organizations/{org_id}/inventory')
def inventory(org_id: UUID, db: DB, user: User, warehouse_id: UUID | None = None, limit: int = Query(25, ge=1, le=100), cursor: str | None = None):
    member(db, org_id, user)
    values = parse_cursor(cursor, 2)
    params = {'org': org_id, 'limit': limit + 1}
    filters = ''
    if warehouse_id:
        filters += ' AND warehouse_id=:warehouse'
        params['warehouse'] = warehouse_id
    if values:
        try:
            params.update(cw=UUID(values[0]), cp=UUID(values[1]))
        except (ValueError, TypeError):
            fail(400, 'INVALID_CURSOR', 'Invalid pagination cursor')
        filters += ' AND (warehouse_id,product_id) > (:cw,:cp)'
    rows = many(db, 'SELECT *,on_hand_qty-reserved_qty AS available_qty FROM inventory WHERE organization_id=:org' + filters + ' ORDER BY warehouse_id,product_id LIMIT :limit', **params)
    next_cursor = page_cursor([rows[limit-1]['warehouse_id'],rows[limit-1]['product_id']]) if len(rows) > limit else None
    return {'items': rows[:limit], 'next_cursor': next_cursor}

def movement(db, org, warehouse, product_id, order_id, kind, on_hand, reserved, user, reason=None):
    db.execute(text('INSERT INTO stock_movements (organization_id,warehouse_id,product_id,order_id,movement_type,delta_on_hand,delta_reserved,actor_user_id,reason) VALUES (:org,:warehouse,:product,:order_id,:kind,:on_hand,:reserved,:user,:reason)'), dict(org=org, warehouse=warehouse, product=product_id, order_id=order_id, kind=kind, on_hand=on_hand, reserved=reserved, user=user['id'], reason=reason))

@app.post('/api/v1/organizations/{org_id}/inventory/adjustments')
def adjust_inventory(org_id: UUID, data: AdjustmentInput, db: DB, user: User):
    member(db, org_id, user, ['admin', 'manager'])
    warehouse = one(db, 'SELECT id,is_active FROM warehouses WHERE organization_id=:org AND id=:id FOR SHARE', org=org_id, id=data.warehouse_id)
    product = one(db, 'SELECT id FROM products WHERE organization_id=:org AND id=:id', org=org_id, id=data.product_id)
    if not warehouse or not product:
        fail(404, 'NOT_FOUND', 'Warehouse or product not found')
    if data.delta_on_hand > 0 and not warehouse['is_active']:
        fail(409, 'INACTIVE_WAREHOUSE', 'Cannot add stock to an inactive warehouse')
    args = dict(org=org_id, warehouse=data.warehouse_id, product=data.product_id, delta=data.delta_on_hand)
    db.execute(text('INSERT INTO inventory (organization_id,warehouse_id,product_id) VALUES (:org,:warehouse,:product) ON CONFLICT DO NOTHING'), args)
    result = one(db, 'UPDATE inventory SET on_hand_qty=on_hand_qty+:delta WHERE organization_id=:org AND warehouse_id=:warehouse AND product_id=:product AND on_hand_qty::bigint+:delta BETWEEN reserved_qty AND 2147483647 RETURNING *,on_hand_qty-reserved_qty AS available_qty', **args)
    if not result:
        fail(409, 'INSUFFICIENT_STOCK', 'Adjustment would violate inventory limits')
    movement(db, org_id, data.warehouse_id, data.product_id, None, 'receipt' if data.delta_on_hand > 0 else 'adjustment', data.delta_on_hand, 0, user, data.reason)
    audit(db, org_id, user, 'inventory.adjusted', 'product', data.product_id, {'delta_on_hand': data.delta_on_hand, 'warehouse_id': str(data.warehouse_id)})
    return result

@app.get('/api/v1/organizations/{org_id}/inventory/movements')
def movements(org_id: UUID, db: DB, user: User, limit: int = Query(25, ge=1, le=100), cursor: str | None = None):
    member(db, org_id, user, ['admin', 'manager'])
    return time_page(db, 'stock_movements', org_id, limit, cursor)

def order_details(db, org_id, order_id, role):
    result = one(db, 'SELECT * FROM orders WHERE organization_id=:org AND id=:id', org=org_id, id=order_id)
    if not result:
        fail(404, 'NOT_FOUND', 'Order not found')
    result['items'] = many(db, 'SELECT product_id,quantity,unit_price_minor,line_total_minor FROM order_items WHERE organization_id=:org AND order_id=:id ORDER BY product_id', org=org_id, id=order_id)
    if role == 'viewer':
        result['customer_email'] = None
    return result

@app.get('/api/v1/organizations/{org_id}/orders')
def orders(org_id: UUID, db: DB, user: User, limit: int = Query(25, ge=1, le=100), cursor: str | None = None, status: Literal['reserved', 'fulfilled', 'cancelled'] | None = None):
    role = member(db, org_id, user)
    result = time_page(db, 'orders', org_id, limit, cursor, ' AND status=:status' if status else '', **({'status': status} if status else {}))
    result['items'] = [order_details(db, org_id, row['id'], role) for row in result['items']]
    return result

@app.get('/api/v1/organizations/{org_id}/orders/{order_id}')
def get_order(org_id: UUID, order_id: UUID, db: DB, user: User):
    return order_details(db, org_id, order_id, member(db, org_id, user))

@app.post('/api/v1/organizations/{org_id}/orders', status_code=201)
def create_order(org_id: UUID, data: OrderInput, response: Response, db: DB, user: User, idempotency_key: Annotated[str | None, Header()] = None):
    role = member(db, org_id, user, ['admin', 'manager', 'staff'])
    if not idempotency_key or not 8 <= len(idempotency_key) <= 128:
        fail(400, 'IDEMPOTENCY_KEY_REQUIRED', 'Idempotency-Key must contain 8 to 128 characters')
    canonical = data.model_dump(mode='json')
    canonical['items'].sort(key=lambda item: item['product_id'])
    request_hash = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    args = {'org': org_id, 'user': user['id'], 'key': idempotency_key, 'hash': request_hash}
    db.execute(text("DELETE FROM idempotency_records WHERE organization_id=:org AND actor_user_id=:user AND method='POST' AND route='/orders' AND key=:key AND expires_at<=now()"), args)
    inserted = one(db, "INSERT INTO idempotency_records (organization_id,actor_user_id,method,route,key,request_hash,expires_at) VALUES (:org,:user,'POST','/orders',:key,:hash,now()+interval '7 days') ON CONFLICT DO NOTHING RETURNING id", **args)
    if not inserted:
        existing = one(db, "SELECT * FROM idempotency_records WHERE organization_id=:org AND actor_user_id=:user AND method='POST' AND route='/orders' AND key=:key FOR UPDATE", **args)
        if not existing or existing['request_hash'] != request_hash:
            fail(409, 'IDEMPOTENCY_CONFLICT', 'Idempotency key was used for a different request')
        if existing['state'] != 'completed':
            fail(409, 'IDEMPOTENCY_PENDING', 'Request is still being processed')
        response.headers['Idempotency-Replayed'] = 'true'
        return existing['response_body']
    warehouse = one(db, 'SELECT id,is_active FROM warehouses WHERE organization_id=:org AND id=:id FOR SHARE', org=org_id, id=data.warehouse_id)
    if not warehouse:
        fail(404, 'NOT_FOUND', 'Warehouse not found')
    if not warehouse['is_active']:
        fail(409, 'INACTIVE_WAREHOUSE', 'Warehouse is inactive')
    org = one(db, 'SELECT default_currency FROM organizations WHERE id=:org', org=org_id)
    order_id = uuid.uuid4()
    lines = []
    total = 0
    for line in sorted(data.items, key=lambda item: str(item.product_id)):
        product = one(db, 'SELECT id,is_active,unit_price_minor FROM products WHERE organization_id=:org AND id=:id FOR SHARE', org=org_id, id=line.product_id)
        if not product:
            fail(404, 'NOT_FOUND', 'Product not found')
        if not product['is_active']:
            fail(409, 'INACTIVE_PRODUCT', 'Product is inactive')
        updated = one(db, 'UPDATE inventory SET reserved_qty=reserved_qty+:quantity WHERE organization_id=:org AND warehouse_id=:warehouse AND product_id=:product AND on_hand_qty-reserved_qty>=:quantity RETURNING product_id', org=org_id, warehouse=data.warehouse_id, product=line.product_id, quantity=line.quantity)
        if not updated:
            fail(409, 'INSUFFICIENT_STOCK', 'Requested quantity exceeds available stock')
        line_total = product['unit_price_minor'] * line.quantity
        total += line_total
        if total > 9223372036854775807:
            fail(422, 'VALIDATION_ERROR', 'Order total exceeds supported amount')
        lines.append((line, product['unit_price_minor'], line_total))
    db.execute(text("INSERT INTO orders (id,organization_id,warehouse_id,created_by,order_number,status,customer_name,customer_email,currency,total_minor) VALUES (:id,:org,:warehouse,:user,:number,'reserved',:name,:email,:currency,:total)"), dict(id=order_id, org=org_id, warehouse=data.warehouse_id, user=user['id'], number='SO-'+order_id.hex.upper(), name=data.customer_name, email=data.customer_email, currency=org['default_currency'], total=total))
    for line, price, line_total in lines:
        db.execute(text('INSERT INTO order_items (organization_id,order_id,product_id,quantity,unit_price_minor,line_total_minor) VALUES (:org,:order_id,:product,:quantity,:price,:total)'), dict(org=org_id, order_id=order_id, product=line.product_id, quantity=line.quantity, price=price, total=line_total))
        movement(db, org_id, data.warehouse_id, line.product_id, order_id, 'reservation', 0, line.quantity, user)
    audit(db, org_id, user, 'order.created', 'order', order_id)
    result = jsonable_encoder(order_details(db, org_id, order_id, role))
    db.execute(text("UPDATE idempotency_records SET state='completed',response_code=201,response_body=CAST(:body AS jsonb) WHERE id=:id"), {'body': json.dumps(result), 'id': inserted['id']})
    response.headers['Idempotency-Replayed'] = 'false'
    return result

def transition_order(org_id, order_id, target, db, user):
    role = member(db, org_id, user, ['admin', 'manager'] if target == 'fulfilled' else ['admin', 'manager', 'staff'])
    order = one(db, 'SELECT * FROM orders WHERE organization_id=:org AND id=:id FOR UPDATE', org=org_id, id=order_id)
    if not order:
        fail(404, 'NOT_FOUND', 'Order not found')
    if role == 'staff' and order['created_by'] != user['id']:
        fail(403, 'FORBIDDEN', 'Staff can cancel only their own orders')
    if order['status'] == target:
        return order_details(db, org_id, order_id, role)
    if order['status'] != 'reserved':
        fail(409, 'ORDER_STATE_CONFLICT', 'Order is already in a terminal state')
    lines = many(db, 'SELECT product_id,quantity FROM order_items WHERE organization_id=:org AND order_id=:id ORDER BY product_id', org=org_id, id=order_id)
    for line in lines:
        on_hand = -line['quantity'] if target == 'fulfilled' else 0
        result = one(db, 'UPDATE inventory SET on_hand_qty=on_hand_qty+:on_hand,reserved_qty=reserved_qty-:quantity WHERE organization_id=:org AND warehouse_id=:warehouse AND product_id=:product AND reserved_qty>=:quantity RETURNING product_id', org=org_id, warehouse=order['warehouse_id'], product=line['product_id'], quantity=line['quantity'], on_hand=on_hand)
        if not result:
            fail(409, 'INVENTORY_CONFLICT', 'Reservation no longer matches inventory')
        movement(db, org_id, order['warehouse_id'], line['product_id'], order_id, 'fulfillment' if target == 'fulfilled' else 'release', on_hand, -line['quantity'], user)
    timestamp_column = 'fulfilled_at' if target == 'fulfilled' else 'cancelled_at'
    db.execute(text(f'UPDATE orders SET status=:status,{timestamp_column}=now() WHERE organization_id=:org AND id=:id'), {'status': target, 'org': org_id, 'id': order_id})
    audit(db, org_id, user, 'order.' + target, 'order', order_id)
    return order_details(db, org_id, order_id, role)

@app.post('/api/v1/organizations/{org_id}/orders/{order_id}/fulfill')
def fulfill_order(org_id: UUID, order_id: UUID, db: DB, user: User):
    return transition_order(org_id, order_id, 'fulfilled', db, user)

@app.post('/api/v1/organizations/{org_id}/orders/{order_id}/cancel')
def cancel_order(org_id: UUID, order_id: UUID, db: DB, user: User):
    return transition_order(org_id, order_id, 'cancelled', db, user)

@app.get('/api/v1/organizations/{org_id}/analytics/overview')
def overview(org_id: UUID, db: DB, user: User):
    member(db, org_id, user)
    counts = one(db, "SELECT count(*) AS total_orders,count(*) FILTER (WHERE status='reserved') AS reserved_orders,count(*) FILTER (WHERE status='fulfilled') AS fulfilled_orders,count(*) FILTER (WHERE status='cancelled') AS cancelled_orders FROM orders WHERE organization_id=:org", org=org_id)
    stock = one(db, 'SELECT count(*) FILTER (WHERE i.on_hand_qty-i.reserved_qty<=p.low_stock_threshold) AS low_stock_positions,coalesce(sum(i.on_hand_qty::numeric*p.unit_price_minor),0) AS stock_value_minor FROM inventory i JOIN products p ON p.organization_id=i.organization_id AND p.id=i.product_id WHERE i.organization_id=:org', org=org_id)
    currency = one(db, 'SELECT default_currency FROM organizations WHERE id=:org', org=org_id)['default_currency']
    return {**counts, **stock, 'currency': currency}

@app.get('/api/v1/organizations/{org_id}/audit-logs')
def audit_logs(org_id: UUID, db: DB, user: User, limit: int = Query(25, ge=1, le=100), cursor: str | None = None):
    member(db, org_id, user, ['admin', 'manager'])
    return time_page(db, 'audit_logs', org_id, limit, cursor)
