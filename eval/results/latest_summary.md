# Evaluation Summary

- **Run:** 2026-06-11 16:00:59
- **Model:** `gemini-2.5-flash`
- **Pass rate (completed questions):** 10/11 (90.9%)
- **Total questions:** 15  ·  **Errored / excluded:** 4
- **Average latency:** 4310.1 ms
- **Total theoretical cost:** $0.014117 (36,473 in / 1,270 out tokens)

## Pass rate by difficulty

| Difficulty | Passed | Total | Pass rate |
| --- | --- | --- | --- |
| easy | 2 | 2 | 100% |
| medium | 5 | 5 | 100% |
| hard | 3 | 4 | 75% |

## Pass rate by category

| Category | Passed | Total | Pass rate |
| --- | --- | --- | --- |
| aggregation | 1 | 1 | 100% |
| cohort | 0 | 1 | 0% |
| count | 1 | 1 | 100% |
| impossible | 1 | 1 | 100% |
| join | 1 | 1 | 100% |
| percentage | 1 | 1 | 100% |
| review | 1 | 1 | 100% |
| time_series | 2 | 2 | 100% |
| top_n | 2 | 2 | 100% |

## Failures (1)

### Q17 (hard/cohort) — How many customers have placed more than one order?
- Failed checks: row_count_in_range
- Row count: 1000
- Error: n/a
- SQL: `SELECT COUNT(DISTINCT c.customer_unique_id) AS customers_with_multiple_orders FROM customers c JOIN orders o ON c.customer_id = o.customer_id GROUP BY c.customer_unique_id HAVING COUNT(o.order_id) > 1`


## Errored — excluded from scoring (4)

Did not complete due to transport/quota errors (e.g. free-tier rate limiting), not model mistakes:

- Q19 (medium/geographic) — What is the total revenue by customer state?
- Q24 (medium/aggregation) — What is the average delivery time in days for delivered orders?
- Q28 (hard/impossible) — Which marketing campaign drove the most sales?
- Q29 (hard/impossible) — What is the profit margin on each product?

