# Demo script

> Fully filled in during S6. This file is a stub so the demo script has a canonical home from S1.

## 60-second demo (target)

1. Dashboard loads, `BRANCH_CURRENT=slow`, p95 line graph is spiking red on `orders.user_id` sequential scans.
2. Click "Suggested index: `CREATE INDEX ix_orders_user_id ON orders(user_id)`".
3. Dashboard calls `POST /branches/switch` → Neon API flips the active branch to `slowquery-fast`.
4. Within 3 seconds, p95 drops from ~1200ms to ~18ms. Graph goes green.
5. README gif shows the whole flow end-to-end.
