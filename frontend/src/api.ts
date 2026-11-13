export type User = { id: string; email: string };
export type Organization = { id: string; name: string; slug: string; default_currency: string; role?: string };
export type Product = { id: string; sku: string; name: string; description?: string; unit_price_minor: number; low_stock_threshold: number; is_active: boolean };
export type Warehouse = { id: string; code: string; name: string; is_active: boolean };
export type Position = { product_id: string; warehouse_id: string; on_hand_qty: number; reserved_qty: number; available_qty: number };
export type Order = { id: string; order_number: string; warehouse_id: string; created_by: string; status: string; customer_name: string; customer_email: string | null; currency: string; total_minor: number; created_at: string; items: {product_id: string; quantity: number; unit_price_minor: number; line_total_minor: number}[] };
export type Movement = { id: string; product_id: string; warehouse_id: string; movement_type: string; delta_on_hand: number; delta_reserved: number; reason: string | null; created_at: string };
export type Audit = { id: string; action: string; actor_user_id: string; entity_type: string; entity_id: string; created_at: string };
export type Member = { user_id: string; email: string; role: string };
export type Overview = { total_orders: number; reserved_orders: number; fulfilled_orders: number; cancelled_orders: number; low_stock_positions: number; stock_value_minor: number; currency: string };
export type Page<T> = { items: T[]; next_cursor: string | null };

let token = '';
let refreshing: Promise<void> | null = null;
export function setToken(value: string) { token = value; }
function csrf() { return decodeURIComponent(document.cookie.split('; ').find(v => v.startsWith('sp_csrf='))?.slice(8) || ''); }
export async function api<T>(path: string, options: RequestInit = {}, retry = true): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body) headers.set('Content-Type', 'application/json');
  if (token) headers.set('Authorization', `Bearer ${token}`);
  if (path === '/auth/refresh' || path === '/auth/logout') headers.set('X-CSRF-Token', csrf());
  const response = await fetch(`/api/v1${path}`, { ...options, headers, credentials: 'include' });
  if (response.status === 401 && token && retry && !path.startsWith('/auth/')) {
    if (!refreshing) refreshing = api<{access_token: string}>('/auth/refresh', { method: 'POST' }, false).then(result => { token = result.access_token; }).finally(() => { refreshing = null; });
    await refreshing;
    return api<T>(path, options, false);
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const detail = data.error?.message || data.detail;
    throw new Error(typeof detail === 'string' ? detail : `Request failed (${response.status})`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
export const send = (body: unknown, method = 'POST'): RequestInit => ({ method, body: JSON.stringify(body) });
