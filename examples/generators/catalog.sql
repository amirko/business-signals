CREATE TABLE categories (
  id text PRIMARY KEY,
  name text NOT NULL
);

CREATE TABLE suppliers (
  id text PRIMARY KEY,
  name text NOT NULL,
  country text NOT NULL,
  base_currency text NOT NULL
);

CREATE TABLE products (
  id text PRIMARY KEY,
  name text NOT NULL,
  category_id text NOT NULL REFERENCES categories(id),
  supplier_id text NOT NULL REFERENCES suppliers(id),
  supplier_unit_cost numeric(10,2) NOT NULL
);

CREATE TABLE stores (
  id text PRIMARY KEY,
  name text NOT NULL,
  city text NOT NULL,
  region text NOT NULL,
  country text NOT NULL,
  channel text NOT NULL
);

CREATE TABLE promotions (
  id text PRIMARY KEY,
  product_id text NOT NULL REFERENCES products(id),
  starts_on date NOT NULL,
  ends_on date NOT NULL,
  discount_pct numeric(5,2) NOT NULL
);

INSERT INTO categories VALUES ('C01', 'Outdoor'), ('C02', 'Home'), ('C03', 'Electronics');
INSERT INTO suppliers VALUES
  ('S17', 'Alpine Supply Co', 'Germany', 'EUR'),
  ('S04', 'Northstar Goods', 'Italy', 'EUR'),
  ('S09', 'Pacific Components', 'United States', 'USD');

INSERT INTO products
SELECT 'P' || lpad(n::text, 3, '0'),
       CASE WHEN n <= 14 THEN 'Trail product ' ELSE 'General product ' END || n,
       CASE WHEN n <= 30 THEN 'C01' WHEN n <= 55 THEN 'C02' ELSE 'C03' END,
       CASE WHEN n <= 14 THEN 'S17' WHEN n % 2 = 0 THEN 'S04' ELSE 'S09' END,
       (18 + n * 1.7)::numeric(10,2)
FROM generate_series(1, 80) AS n;

INSERT INTO stores VALUES
  ('IT-MIL-01', 'Milano Centro', 'Milan', 'Lombardy', 'Italy', 'store'),
  ('IT-TUR-01', 'Torino Porta', 'Turin', 'Piedmont', 'Italy', 'store'),
  ('IT-BOL-01', 'Bologna Centro', 'Bologna', 'Emilia-Romagna', 'Italy', 'store'),
  ('IT-WEB-01', 'Italy Online', 'Online', 'National', 'Italy', 'online');
