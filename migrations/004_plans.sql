ALTER TABLE pricing_plans ADD COLUMN panel_scope TEXT NOT NULL DEFAULT 'selected';
ALTER TABLE pricing_plans ADD COLUMN sale_status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE pricing_plans ADD COLUMN first_purchase_only INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pricing_plans ADD COLUMN revision INTEGER NOT NULL DEFAULT 1;
UPDATE pricing_plans SET sale_status=CASE WHEN is_active=1 THEN 'active' ELSE 'hidden' END;
CREATE TABLE plan_panels (
 plan_code TEXT NOT NULL REFERENCES pricing_plans(plan_code), panel_id INTEGER NOT NULL REFERENCES panels(id),
 allowed INTEGER NOT NULL CHECK(allowed IN(0,1)), PRIMARY KEY(plan_code,panel_id)
);
CREATE TABLE tier_policies (
 actor_type TEXT NOT NULL CHECK(actor_type IN('rep','end_user')), tier_code TEXT NOT NULL,
 title_fa TEXT NOT NULL, rank INTEGER NOT NULL, discount_bps INTEGER NOT NULL DEFAULT 0 CHECK(discount_bps BETWEEN 0 AND 10000),
 cashback_bps INTEGER NOT NULL DEFAULT 0 CHECK(cashback_bps BETWEEN 0 AND 10000),
 daily_test_quota INTEGER NOT NULL DEFAULT 1, default_credit_limit INTEGER NOT NULL DEFAULT 0,
 min_monthly_sales_toman INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(actor_type,tier_code)
);
CREATE TABLE rep_custom_prices (
 seller_key TEXT NOT NULL, buyer_rep_id INTEGER NOT NULL REFERENCES representatives(id),
 plan_code TEXT NOT NULL REFERENCES pricing_plans(plan_code), price_toman INTEGER NOT NULL CHECK(price_toman>=0),
 revision INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(seller_key,buyer_rep_id,plan_code)
);
