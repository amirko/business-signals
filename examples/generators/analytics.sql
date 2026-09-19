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

SELECT setseed(0.4217);

INSERT INTO inventory_history
SELECT day, product, store,
       CASE WHEN day >= '2024-07-08' AND n <= 14 AND store <> 'IT-WEB-01'
            THEN greatest(0, 22 - ((day::date - DATE '2024-07-08') * 3))
            ELSE 80 + floor(random() * 25)::int END,
       CASE WHEN day >= '2024-07-08' AND n <= 14 THEN 0 ELSE 18 END
FROM generate_series('2024-06-15'::timestamptz, '2024-08-10', '1 day') AS day
CROSS JOIN LATERAL (SELECT n, 'P' || lpad(n::text, 3, '0') AS product FROM generate_series(1, 30) n) p
CROSS JOIN unnest(ARRAY['IT-MIL-01','IT-TUR-01','IT-BOL-01','IT-WEB-01']) AS store;

INSERT INTO sales_events
SELECT day + make_interval(hours => sale_no % 12),
       'E-' || to_char(day, 'YYYYMMDD') || '-' || product || '-' || store || '-' || sale_no,
       product, store,
       CASE WHEN day >= '2024-07-14' AND day < '2024-08-01' AND n <= 14 AND store IN ('IT-MIL-01','IT-TUR-01') THEN 1 ELSE 2 + floor(random() * 3)::int END,
       CASE WHEN day >= '2024-07-14' AND day < '2024-08-01' AND n <= 14 AND store IN ('IT-MIL-01','IT-TUR-01') THEN 38 ELSE 95 + floor(random() * 35) END,
       CASE WHEN store = 'IT-WEB-01' THEN 'online' ELSE 'store' END,
       CASE WHEN store = 'IT-WEB-01' THEN 'web' END, 'card', 'completed', '4.16'
FROM generate_series('2024-06-15'::timestamptz, '2024-08-10', '1 day') AS day
CROSS JOIN LATERAL (SELECT n, 'P' || lpad(n::text, 3, '0') AS product FROM generate_series(1, 30) n) p
CROSS JOIN unnest(ARRAY['IT-MIL-01','IT-TUR-01','IT-BOL-01','IT-WEB-01']) AS store
CROSS JOIN generate_series(1, 3) AS sale_no;

INSERT INTO conversion_metrics
SELECT hour, platform, 900, 320,
       CASE WHEN hour >= '2024-03-18' AND platform = 'ios' THEN 120 ELSE 250 END,
       CASE WHEN hour >= '2024-03-18' AND platform = 'ios' THEN 175 ELSE 12 END,
       CASE WHEN hour >= '2024-03-18' THEN '4.17' ELSE '4.16' END
FROM generate_series('2024-03-10'::timestamptz, '2024-03-25', '1 hour') AS hour
CROSS JOIN unnest(ARRAY['ios','android','web']) AS platform;

INSERT INTO store_traffic
SELECT day, store, 700 + floor(random() * 100)::int
FROM generate_series('2024-06-15'::timestamptz, '2024-08-10', '1 day') AS day
CROSS JOIN unnest(ARRAY['IT-MIL-01','IT-TUR-01','IT-BOL-01','IT-WEB-01']) AS store;

INSERT INTO price_history
SELECT day, 'P' || lpad(n::text, 3, '0'), 129.00,
       CASE WHEN day >= '2024-09-01' AND n > 55 THEN 91.00 ELSE 78.00 END,
       72.00, CASE WHEN n > 55 THEN 'USD' ELSE 'EUR' END
FROM generate_series('2024-08-01'::timestamptz, '2024-10-01', '1 day') AS day
CROSS JOIN generate_series(1, 80) n;

CREATE INDEX ON sales_events (product_id, occurred_at DESC);
CREATE INDEX ON inventory_history (product_id, recorded_at DESC);
CREATE INDEX ON conversion_metrics (platform, recorded_at DESC);
