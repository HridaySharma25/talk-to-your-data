# Olist E-Commerce Database — Schema

SQLite database of a Brazilian e-commerce marketplace. Tables, columns, keys, and relationships follow.

## Tables

### `orders` (99,441 rows)
One row per customer order — the hub table linking customers to their items, payments, and reviews.

| Column | Type | Key | Description |
| --- | --- | --- | --- |
| order_id | TEXT | PK | Unique order identifier. |
| customer_id | TEXT | FK -> customers.customer_id | Customer who placed the order. |
| order_status | TEXT |  | Order lifecycle status; one of: approved, canceled, created, delivered, invoiced, processing, shipped, unavailable. Most revenue analysis filters to 'delivered'. |
| order_purchase_timestamp | DATETIME |  | When the order was placed — the primary time dimension for trends and cohorts. |
| order_approved_at | DATETIME |  | When payment was approved (nullable). |
| order_delivered_carrier_date | DATETIME |  | When the order was handed to the logistics carrier (nullable). |
| order_delivered_customer_date | DATETIME |  | When the order reached the customer (nullable); pair with purchase timestamp for delivery time. |
| order_estimated_delivery_date | DATETIME |  | Delivery date promised to the customer at purchase; compare with the actual delivery date for on-time analysis. |

### `order_items` (112,650 rows)
One row per line item within an order (an order with N items has N rows). Product revenue lives here.

| Column | Type | Key | Description |
| --- | --- | --- | --- |
| order_id | TEXT | PK, FK -> orders.order_id | Order this line item belongs to. |
| order_item_id | INTEGER | PK | Sequential line number within the order (1, 2, 3 ...). MAX per order = number of items. |
| product_id | TEXT | FK -> products.product_id | Product sold on this line. |
| seller_id | TEXT | FK -> sellers.seller_id | Seller fulfilling this line. |
| shipping_limit_date | DATETIME |  | Seller's deadline to hand the item to the carrier. |
| price | REAL |  | Item price in BRL (Brazilian Real). SUM(price) = product revenue. |
| freight_value | REAL |  | Shipping cost charged for this item, in BRL. |

### `order_payments` (103,886 rows)
One row per payment transaction on an order; a single order may be split across multiple payment rows.

| Column | Type | Key | Description |
| --- | --- | --- | --- |
| order_id | TEXT | PK, FK -> orders.order_id | Order being paid. |
| payment_sequential | INTEGER | PK | Sequence number when an order is paid across multiple transactions. |
| payment_type | TEXT |  | Payment method; one of: boleto, credit_card, debit_card, not_defined, voucher. |
| payment_installments | INTEGER |  | Number of installments chosen by the customer (1–24). |
| payment_value | REAL |  | Transaction amount in BRL. SUM(payment_value) = total paid by the customer (includes freight). |

### `order_reviews` (99,224 rows)
Customer satisfaction reviews. review_score drives CSAT-style analysis.

| Column | Type | Key | Description |
| --- | --- | --- | --- |
| review_id | TEXT | PK | Review identifier (not unique on its own; some reviews recur). |
| order_id | TEXT | PK, FK -> orders.order_id | Order being reviewed. |
| review_score | INTEGER |  | Customer rating from 1 (worst) to 5 (best). AVG(review_score) or its distribution = satisfaction. |
| review_comment_title | TEXT |  | Optional free-text review title (often NULL; Portuguese). |
| review_comment_message | TEXT |  | Optional free-text review body (often NULL; Portuguese). |
| review_creation_date | DATETIME |  | When the review survey was sent to the customer. |
| review_answer_timestamp | DATETIME |  | When the customer submitted the review. |

### `products` (32,951 rows)
Product catalog with physical attributes. Category names are Portuguese — join the translation table for English.

| Column | Type | Key | Description |
| --- | --- | --- | --- |
| product_id | TEXT | PK | Unique product identifier. |
| product_category_name | TEXT | FK -> product_category_name_translation.product_category_name | Product category in Portuguese (≈610 NULLs). Join the translation table for the English name. |
| product_name_length | INTEGER |  | Character count of the product name (nullable). |
| product_description_length | INTEGER |  | Character count of the product description (nullable). |
| product_photos_qty | INTEGER |  | Number of published product photos (nullable). |
| product_weight_g | REAL |  | Product weight in grams. |
| product_length_cm | REAL |  | Package length in centimeters. |
| product_height_cm | REAL |  | Package height in centimeters. |
| product_width_cm | REAL |  | Package width in centimeters. |

### `product_category_name_translation` (71 rows)
Lookup mapping each Portuguese product_category_name to its English label.

| Column | Type | Key | Description |
| --- | --- | --- | --- |
| product_category_name | TEXT | PK | Portuguese category name (matches products.product_category_name). |
| product_category_name_english | TEXT |  | English category label — prefer this in results for readability. |

### `customers` (99,441 rows)
Customer records. customer_id is per-order; customer_unique_id identifies the real person across orders.

| Column | Type | Key | Description |
| --- | --- | --- | --- |
| customer_id | TEXT | PK | Per-order customer key (a NEW value is issued for each order). |
| customer_unique_id | TEXT |  | Stable identifier for the real person across orders. Use COUNT(DISTINCT customer_unique_id) to count customers and to find repeat buyers. |
| customer_zip_code_prefix | INTEGER |  | First digits of the customer's zip code (join to geolocation). |
| customer_city | TEXT |  | Customer city (lowercase, Portuguese). |
| customer_state | TEXT |  | Customer state as a 2-letter Brazilian code (e.g. SP, RJ, MG). |

### `sellers` (3,095 rows)
Marketplace sellers who fulfill order items.

| Column | Type | Key | Description |
| --- | --- | --- | --- |
| seller_id | TEXT | PK | Unique seller identifier. |
| seller_zip_code_prefix | INTEGER |  | First digits of the seller's zip code (join to geolocation). |
| seller_city | TEXT |  | Seller city (lowercase, Portuguese). |
| seller_state | TEXT |  | Seller state as a 2-letter Brazilian code. |

### `geolocation` (1,000,163 rows)
Latitude/longitude points per zip-code prefix (many rows per prefix). Use for geographic analysis.

| Column | Type | Key | Description |
| --- | --- | --- | --- |
| geolocation_zip_code_prefix | INTEGER |  | First digits of a Brazilian zip code; MANY rows per prefix. |
| geolocation_lat | REAL |  | Latitude coordinate. |
| geolocation_lng | REAL |  | Longitude coordinate. |
| geolocation_city | TEXT |  | City for the coordinate (lowercase, Portuguese). |
| geolocation_state | TEXT |  | State for the coordinate (2-letter Brazilian code). |

## Join paths

- orders.customer_id = customers.customer_id
- order_items.order_id = orders.order_id
- order_items.product_id = products.product_id
- order_items.seller_id = sellers.seller_id
- order_payments.order_id = orders.order_id
- order_reviews.order_id = orders.order_id
- products.product_category_name = product_category_name_translation.product_category_name
- customers.customer_zip_code_prefix = geolocation.geolocation_zip_code_prefix
- sellers.seller_zip_code_prefix = geolocation.geolocation_zip_code_prefix

## Dataset facts & modelling notes

- Orders span **2016-09-04** to **2018-10-17** (2016 and late-2018 are sparse).
- All monetary values (price, freight_value, payment_value) are in BRL (Brazilian Real).
- Product revenue = SUM(order_items.price). Total amount paid (incl. freight) = SUM(order_payments.payment_value).
- Dates are stored as ISO-8601 text. Use SQLite date functions: strftime('%Y', col) for year, strftime('%Y-%m', col) for month, date(col) for the day, and julianday(b) - julianday(a) for differences in days.
- Count customers with COUNT(DISTINCT customer_unique_id) — NOT customer_id, which changes per order.
- Category names in `products` are Portuguese; join product_category_name_translation and return the English label.
- `geolocation` has multiple rows per zip prefix and no primary key — aggregate (e.g. AVG lat/lng) or pick one row when joining to avoid inflating counts.
- Not every order has items, payments, or reviews; choose INNER vs LEFT JOIN deliberately.
- review_id is not unique (≈814 duplicate rows) and an order can have multiple reviews.
