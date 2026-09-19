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
  channel text NOT NULL CHECK (channel IN ('store', 'online'))
);

CREATE TABLE promotions (
  id text PRIMARY KEY,
  product_id text NOT NULL REFERENCES products(id),
  starts_on date NOT NULL,
  ends_on date NOT NULL,
  discount_pct numeric(5,2) NOT NULL
);

\copy categories FROM '/data/catalog/categories.csv' WITH (FORMAT csv, HEADER true)
\copy suppliers FROM '/data/catalog/suppliers.csv' WITH (FORMAT csv, HEADER true)
\copy products FROM '/data/catalog/products.csv' WITH (FORMAT csv, HEADER true)
\copy stores FROM '/data/catalog/stores.csv' WITH (FORMAT csv, HEADER true)
\copy promotions FROM '/data/catalog/promotions.csv' WITH (FORMAT csv, HEADER true)
