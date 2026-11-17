-- The demo shop: a small appliance store's customers, orders and returns.
--
-- This is the BACKDROP for the WhatsApp support demo, not its subject. It
-- exists so the agent has something true to say when a real phone asks
-- "where is my order" and something real to write when it starts a return.
--
-- Run through scripts/seed_demo_shop.sh, which connects as the compose
-- superuser, supplies the role password as the psql variable :pw, and
-- prints the connection string to put in .env. Idempotent: it drops and
-- recreates the whole database, so it doubles as the reset between demos.
--
-- Least privilege is the point of the role, not a nicety. shop_agent can
-- SELECT from every table and INSERT into returns, and nothing else. The
-- agent's one write is therefore the only write the database will accept
-- from it, whatever the model asks for.
--
-- Dates are relative to now() so the demo does not age: an order that
-- "arrived last Friday" still did, whenever this is run.

\set ON_ERROR_STOP on

-- Kick any lingering connection so the drop cannot fail mid-demo.
DROP DATABASE IF EXISTS shopdemo WITH (FORCE);
DROP ROLE IF EXISTS shop_agent;

CREATE ROLE shop_agent LOGIN PASSWORD :'pw';
CREATE DATABASE shopdemo;
GRANT CONNECT ON DATABASE shopdemo TO shop_agent;

\connect shopdemo

-- ─── schema ──────────────────────────────────────────────────────────────────

CREATE TABLE customers (
    id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    first_name  text NOT NULL,
    last_name   text NOT NULL,
    email       text NOT NULL UNIQUE,
    -- Digits only, country code first, no plus: exactly how Meta reports
    -- a WhatsApp sender, so the lookup is a plain equality.
    phone       text NOT NULL UNIQUE,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE products (
    id                  integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku                 text NOT NULL UNIQUE,
    name                text NOT NULL,
    category            text NOT NULL,
    price_cents         integer NOT NULL CHECK (price_cents >= 0),
    return_window_days  integer NOT NULL DEFAULT 30,
    -- Pictures for the WhatsApp photo bubbles and the widget cards. Meta
    -- fetches image_url itself, so it must be a public HTTPS link (these
    -- are Unsplash). fallback_image_url is a copy you host, for example a
    -- public Supabase Storage bucket, used when the first cannot be fetched.
    -- Both optional: a shop without them simply sends no pictures.
    image_url           text,
    fallback_image_url  text
);

CREATE TABLE orders (
    id            integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_ref     text NOT NULL UNIQUE,
    customer_id   integer NOT NULL REFERENCES customers(id),
    status        text NOT NULL
                  CHECK (status IN ('packing', 'shipped', 'delivered', 'cancelled')),
    placed_at     timestamptz NOT NULL,
    delivered_at  timestamptz,
    total_cents   integer NOT NULL CHECK (total_cents >= 0)
);
CREATE INDEX orders_customer_idx ON orders (customer_id);

CREATE TABLE order_items (
    id          integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id    integer NOT NULL REFERENCES orders(id),
    product_id  integer NOT NULL REFERENCES products(id),
    qty         integer NOT NULL CHECK (qty > 0),
    unit_cents  integer NOT NULL CHECK (unit_cents >= 0)
);
CREATE INDEX order_items_order_idx ON order_items (order_id);

-- Human-readable return references, RET-5001 onward. The table's default
-- reads this, and Postgres resolves that name when the table is created,
-- so the sequence has to exist first.
CREATE SEQUENCE returns_ref_seq START 5001;

CREATE TABLE returns (
    id           integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    return_ref   text NOT NULL UNIQUE DEFAULT ('RET-' || to_char(nextval('returns_ref_seq'), 'FM0000')),
    order_id     integer NOT NULL REFERENCES orders(id),
    customer_id  integer NOT NULL REFERENCES customers(id),
    reason       text NOT NULL,
    -- What the agent saw in the photo, in its own words. Text, never a file.
    note         text,
    status       text NOT NULL DEFAULT 'requested'
                 CHECK (status IN ('requested', 'approved', 'received', 'refunded')),
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX returns_customer_idx ON returns (customer_id);
-- One open return per order: the desk refuses a second, and so does the DB.
CREATE UNIQUE INDEX returns_one_open_per_order
    ON returns (order_id) WHERE status IN ('requested', 'approved');

-- ─── the role's reach ────────────────────────────────────────────────────────

GRANT USAGE ON SCHEMA public TO shop_agent;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO shop_agent;
GRANT INSERT ON returns TO shop_agent;
-- An INSERT needs the identity and reference sequences; nothing else does.
GRANT USAGE, SELECT ON SEQUENCE returns_id_seq TO shop_agent;
GRANT USAGE, SELECT ON SEQUENCE returns_ref_seq TO shop_agent;

-- ─── products ────────────────────────────────────────────────────────────────
-- Two washers on purpose. Two items sharing a word is the case that caught
-- the live calls: asked about "the top load washer", the agent answered
-- about the front load one. The data has to contain the trap or the
-- asking-which-one path is never exercised.

INSERT INTO products (sku, name, category, price_cents, return_window_days, image_url) VALUES
    ('WASH-SAM-FL', 'Samsung front load washer',        'laundry',  74900, 30,
     'https://images.unsplash.com/photo-1626806819282-2c1dc01a5e0c?fm=jpg&fit=crop&w=800&q=70'),
    ('WASH-WHP-TL', 'Whirlpool top load washer',        'laundry',  59900, 30,
     'https://images.unsplash.com/photo-1622473590925-e3616c0a41bf?fm=jpg&fit=crop&w=800&q=70'),
    ('DISH-BSH-24', 'Bosch 24 inch dishwasher',         'kitchen',  84900, 30,
     'https://images.unsplash.com/photo-1581622558663-b2e33377dfb2?fm=jpg&fit=crop&w=800&q=70'),
    ('MICR-PAN-CT', 'Panasonic countertop microwave',   'kitchen',  17900, 14,
     'https://images.unsplash.com/photo-1585659722983-3a675dabf23d?fm=jpg&fit=crop&w=800&q=70'),
    ('FRDG-LG-FD',  'LG french door refrigerator',      'kitchen', 189900, 30,
     'https://images.unsplash.com/photo-1721613877687-c9099b698faa?fm=jpg&fit=crop&w=800&q=70'),
    ('KETL-BRV-17', 'Breville 1.7 litre kettle',        'small',     8900, 14,
     'https://images.unsplash.com/photo-1748408082799-94daff13e792?fm=jpg&fit=crop&w=800&q=70'),
    ('TOAS-CUI-4S', 'Cuisinart 4 slice toaster',        'small',     6900, 14,
     'https://images.unsplash.com/photo-1686644823126-7ed947386b77?fm=jpg&fit=crop&w=800&q=70'),
    ('VAC-DYS-V15', 'Dyson V15 cordless vacuum',        'cleaning', 74900, 30,
     'https://images.unsplash.com/photo-1765970101654-337b573142fb?fm=jpg&fit=crop&w=800&q=70');

-- The hosted fallback copies, if you keep some: one UPDATE per SKU, e.g.
-- UPDATE products SET fallback_image_url =
--   'https://PROJECT.supabase.co/storage/v1/object/public/BUCKET/WASH-SAM-FL.jpg'
--   WHERE sku = 'WASH-SAM-FL';

-- ─── customers ───────────────────────────────────────────────────────────────
-- The first three are the phones registered as Meta test recipients, so
-- identification works from a real handset. Only the first has messaged the
-- number so far; the other two are PLACEHOLDERS to replace with the real
-- registered numbers before a class (digits only, country code first).
-- The rest are dummies who never message, so the tables look like a shop
-- rather than a toy.

INSERT INTO customers (first_name, last_name, email, phone) VALUES
    ('Usama',  'Shami',    'usama@example.com',   '923358370304'),  -- registered
    ('Amina',  'Raza',     'amina@example.com',   '923000000002'),  -- PLACEHOLDER
    ('Bilal',  'Ahmed',    'bilal@example.com',   '923000000003'),  -- PLACEHOLDER
    ('Sara',   'Khan',     'sara@example.com',    '15550000004'),
    ('Daniel', 'Okafor',   'daniel@example.com',  '15550000005'),
    ('Mei',    'Tanaka',   'mei@example.com',     '15550000006'),
    ('Jonas',  'Berg',     'jonas@example.com',   '15550000007');

-- ─── orders ──────────────────────────────────────────────────────────────────
-- Persona 1 (Usama): the return story. Both washers delivered inside their
-- window (the disambiguation trap), a dishwasher on its way, a microwave
-- still being packed.
-- Persona 2 (Amina): a return already open, so "what happened to my return"
-- has an answer on day one; plus one delivered kettle.
-- Persona 3 (Bilal): a delivered fridge OUTSIDE its window (too late to
-- return is a real answer the agent must give) and a cancelled order.
-- Dummies: assorted, one order each.

INSERT INTO orders (order_ref, customer_id, status, placed_at, delivered_at, total_cents) VALUES
    ('ORD-1023', 1, 'delivered', now() - interval '12 days', now() - interval '6 days',  74900),  -- Samsung front load
    ('ORD-1031', 1, 'delivered', now() - interval '20 days', now() - interval '9 days',  59900),  -- Whirlpool top load
    ('ORD-1047', 1, 'shipped',   now() - interval '3 days',  NULL,                        84900),  -- Bosch dishwasher
    ('ORD-1052', 1, 'packing',   now() - interval '1 day',   NULL,                        17900),  -- microwave
    ('ORD-1018', 2, 'delivered', now() - interval '16 days', now() - interval '10 days', 74900),  -- Dyson, return open
    ('ORD-1039', 2, 'delivered', now() - interval '9 days',  now() - interval '4 days',   8900),  -- kettle
    ('ORD-0987', 3, 'delivered', now() - interval '52 days', now() - interval '45 days', 189900), -- fridge, window passed
    ('ORD-1044', 3, 'cancelled', now() - interval '5 days',  NULL,                         6900),  -- toaster, cancelled
    ('ORD-1029', 4, 'delivered', now() - interval '11 days', now() - interval '7 days',   6900),
    ('ORD-1035', 5, 'shipped',   now() - interval '4 days',  NULL,                        74900),
    ('ORD-1041', 6, 'delivered', now() - interval '8 days',  now() - interval '2 days',   8900),
    ('ORD-1050', 7, 'packing',   now() - interval '1 day',   NULL,                       189900);

INSERT INTO order_items (order_id, product_id, qty, unit_cents)
SELECT o.id, p.id, 1, p.price_cents
FROM (VALUES
    ('ORD-1023', 'WASH-SAM-FL'),
    ('ORD-1031', 'WASH-WHP-TL'),
    ('ORD-1047', 'DISH-BSH-24'),
    ('ORD-1052', 'MICR-PAN-CT'),
    ('ORD-1018', 'VAC-DYS-V15'),
    ('ORD-1039', 'KETL-BRV-17'),
    ('ORD-0987', 'FRDG-LG-FD'),
    ('ORD-1044', 'TOAS-CUI-4S'),
    ('ORD-1029', 'TOAS-CUI-4S'),
    ('ORD-1035', 'VAC-DYS-V15'),
    ('ORD-1041', 'KETL-BRV-17'),
    ('ORD-1050', 'FRDG-LG-FD')
) AS v(order_ref, sku)
JOIN orders o ON o.order_ref = v.order_ref
JOIN products p ON p.sku = v.sku;

-- ─── a return that already exists ────────────────────────────────────────────

INSERT INTO returns (order_id, customer_id, reason, note, status, created_at)
SELECT o.id, o.customer_id,
       'stopped charging after two days',
       'Customer sent a photo of the charging light not coming on.',
       'approved',
       now() - interval '3 days'
FROM orders o WHERE o.order_ref = 'ORD-1018';

-- ─── a quick self-check, printed by the script ───────────────────────────────

SELECT (SELECT count(*) FROM customers)   AS customers,
       (SELECT count(*) FROM products)    AS products,
       (SELECT count(*) FROM orders)      AS orders,
       (SELECT count(*) FROM order_items) AS items,
       (SELECT count(*) FROM returns)     AS returns;
