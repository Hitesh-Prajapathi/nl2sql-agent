-- ============================================================
-- NL-to-SQL Agent Assignment — Schema
-- Domain: E-commerce order/payment/support platform
-- ============================================================

CREATE TABLE customers (
    customer_id     SERIAL PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    email           VARCHAR(150) NOT NULL,
    city            VARCHAR(100),
    tier            VARCHAR(20) CHECK (tier IN ('Basic','Silver','Gold')),
    signup_date     DATE NOT NULL
);

CREATE TABLE products (
    product_id      SERIAL PRIMARY KEY,
    sku             VARCHAR(30) UNIQUE NOT NULL,
    name            VARCHAR(150) NOT NULL,
    category        VARCHAR(50) NOT NULL,
    unit_price      NUMERIC(10,2) NOT NULL
);

CREATE TABLE agents (
    agent_id        SERIAL PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    team            VARCHAR(50) NOT NULL
);

-- orders.status lifecycle: placed -> shipped -> delivered, or cancelled / returned
CREATE TABLE orders (
    order_id        SERIAL PRIMARY KEY,
    customer_id     INTEGER NOT NULL REFERENCES customers(customer_id),
    order_date      DATE NOT NULL,
    status          VARCHAR(20) NOT NULL CHECK (status IN ('placed','shipped','delivered','cancelled','returned')),
    shipping_city   VARCHAR(100),
    total_amount    NUMERIC(10,2) NOT NULL  -- order-line total at time of order; may not equal amount actually paid
);

CREATE TABLE order_items (
    order_item_id   SERIAL PRIMARY KEY,
    order_id        INTEGER NOT NULL REFERENCES orders(order_id),
    product_id      INTEGER NOT NULL REFERENCES products(product_id),
    quantity        INTEGER NOT NULL,
    unit_price      NUMERIC(10,2) NOT NULL  -- price at time of purchase, may differ from products.unit_price today
);

-- payments.status lifecycle: pending -> completed / failed; completed -> refunded
CREATE TABLE payments (
    payment_id      SERIAL PRIMARY KEY,
    order_id        INTEGER NOT NULL REFERENCES orders(order_id),
    payment_date    DATE NOT NULL,
    amount          NUMERIC(10,2) NOT NULL,  -- actual amount charged; can be less than orders.total_amount on partial payment
    method          VARCHAR(20) NOT NULL CHECK (method IN ('card','upi','netbanking','wallet')),
    status          VARCHAR(20) NOT NULL CHECK (status IN ('pending','completed','failed','refunded'))
);

CREATE TABLE refunds (
    refund_id       SERIAL PRIMARY KEY,
    payment_id      INTEGER NOT NULL REFERENCES payments(payment_id),
    refund_date     DATE NOT NULL,
    amount          NUMERIC(10,2) NOT NULL,  -- may be a partial refund, not always equal to payments.amount
    reason          VARCHAR(100)
);

-- support_tickets.status lifecycle: open -> escalated / closed  (independent of orders.status and payments.status)
CREATE TABLE support_tickets (
    ticket_id       SERIAL PRIMARY KEY,
    customer_id     INTEGER NOT NULL REFERENCES customers(customer_id),
    order_id        INTEGER REFERENCES orders(order_id),  -- nullable: not every ticket is order-related
    agent_id        INTEGER REFERENCES agents(agent_id),
    created_date    DATE NOT NULL,
    category        VARCHAR(30) NOT NULL CHECK (category IN ('billing','shipping','product','account','other')),
    status          VARCHAR(20) NOT NULL CHECK (status IN ('open','escalated','closed'))
);
