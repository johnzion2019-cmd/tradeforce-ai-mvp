# TradeForce AI — Phase 5

Phase 5 turns the Phase 4 marketplace into a monetization-ready product.

## Contractor plans

- Starter — free marketplace entry, job posting and basic recruiting workflow.
- Pro — paid monthly plan for advanced talent sourcing, saved candidates, messaging and recruiting workflow.
- Business — paid monthly plan for teams that need higher-volume recruiting.

Pricing is intentionally configured through Stripe Price IDs rather than hard-coded dollar amounts in application logic.

## Billing architecture

Phase 5 uses Stripe Checkout for subscription purchase and Stripe Customer Portal for self-service billing. Subscription state is synchronized through signed Stripe webhooks. Secrets and Price IDs must be supplied through Render environment variables and must never be committed to GitHub.

Required production variables:

- STRIPE_SECRET_KEY
- STRIPE_WEBHOOK_SECRET
- STRIPE_PRO_PRICE_ID
- STRIPE_BUSINESS_PRICE_ID
- APP_BASE_URL
- SESSION_SECRET

## Production launch gates

Before accepting real payments: rotate the existing admin password, set a dedicated SESSION_SECRET, configure Stripe in test mode first, configure and test the webhook endpoint, verify cancellation/renewal flows, add Terms of Service and Privacy Policy, configure durable object storage, add rate limiting/CSRF protections, and run end-to-end worker/contractor/billing tests.

## Revenue model

Primary Phase 5 revenue is contractor SaaS subscriptions. Placement fees or staffing markups can be layered on later after legal, payroll, insurance and staffing-compliance requirements are addressed.