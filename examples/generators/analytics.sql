CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE sales_events (
  occurred_at timestamptz NOT NULL,
  event_id text NOT NULL,
  product_id text NOT NULL,
  store_id text NOT NULL,
  units integer NOT NULL,
  revenue numeric(12,2) NOT NULL,
  channel text NOT NULL,
  platform text,
  payment_method text,
  payment_status text,
  app_release text
);
SELECT create_hypertable('sales_events', by_range('occurred_at'));

CREATE TABLE inventory_history (
  recorded_at timestamptz NOT NULL,
  product_id text NOT NULL,
  store_id text NOT NULL,
  available_units integer NOT NULL,
  incoming_units integer NOT NULL DEFAULT 0
);
SELECT create_hypertable('inventory_history', by_range('recorded_at'));

CREATE TABLE price_history (
  recorded_at timestamptz NOT NULL,
  product_id text NOT NULL,
  selling_price numeric(10,2) NOT NULL,
  local_unit_cost numeric(10,2) NOT NULL,
  supplier_base_price numeric(10,2) NOT NULL,
  currency text NOT NULL
);
SELECT create_hypertable('price_history', by_range('recorded_at'));

CREATE TABLE store_traffic (
  recorded_at timestamptz NOT NULL,
  store_id text NOT NULL,
  visits integer NOT NULL
);
SELECT create_hypertable('store_traffic', by_range('recorded_at'));

CREATE TABLE conversion_metrics (
  recorded_at timestamptz NOT NULL,
  platform text NOT NULL,
  sessions integer NOT NULL,
  checkout_sessions integer NOT NULL,
  completed_orders integer NOT NULL,
  payment_failures integer NOT NULL,
  app_release text NOT NULL
);
SELECT create_hypertable('conversion_metrics', by_range('recorded_at'));

\copy sales_events FROM '/data/analytics/sales_events.csv' WITH (FORMAT csv, HEADER true)
\copy inventory_history FROM '/data/analytics/inventory_history.csv' WITH (FORMAT csv, HEADER true)
\copy price_history FROM '/data/analytics/price_history.csv' WITH (FORMAT csv, HEADER true)
\copy store_traffic FROM '/data/analytics/store_traffic.csv' WITH (FORMAT csv, HEADER true)
\copy conversion_metrics FROM '/data/analytics/conversion_metrics.csv' WITH (FORMAT csv, HEADER true)

CREATE INDEX ON sales_events (product_id, occurred_at DESC);
CREATE INDEX ON inventory_history (product_id, recorded_at DESC);
CREATE INDEX ON conversion_metrics (platform, recorded_at DESC);
