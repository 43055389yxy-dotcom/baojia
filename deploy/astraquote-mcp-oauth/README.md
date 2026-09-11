# AstraQuote OAuth gateway

The gateway is packaged inside the single AstraQuote production container. It
publishes `https://pricing-mcp.tontiancloud.com/mcp` and proxies authenticated
requests to the loopback-only AstraQuote MCP process.

The OAuth dynamic-registration boundary accepts the approved ChatGPT HTTPS
callbacks, WorkBuddy packaged-connector callbacks, and WorkBuddy custom-MCP
callbacks such as
`workbuddy://workbuddy/mcp/custom-mcp%3AAstraQuote/oauth/callback`. It also
accepts WorkBuddy's fallback callback only on
`http://127.0.0.1:<dynamic-port>/oauth/callback`; non-loopback HTTP callbacks
remain rejected. Both clients use OAuth 2.1 authorization code flow with PKCE.

The public MCP exposes exactly seven tools:

1. `describe_service`
2. `get_attribute_values`
3. `get_prices`
4. `get_price_results`
5. `get_quote_job_status`
6. `resume_quote_job`
7. `build_estimate`

The first six tools require `pricing:read`; `build_estimate` requires
`pricing:write`. Unknown future tools fail closed as writes.

The runtime does not contain a cloud calculator or a browser. `get_prices`
dispatches only to the selected provider's official price catalog. GPT selects
the returned SKU/price identities and calculates the quote; `build_estimate`
performs mechanical evidence, fact-coverage and sum checks before page or Excel
delivery.

Production configuration is read from:

- `/home/ec2-user/astraquote/config/backend.env`
- `/home/ec2-user/astraquote/config/mcp.env`
- `/home/ec2-user/astraquote/config/oauth.env`

OAuth client registrations and refresh tokens live in
`/home/ec2-user/astraquote/data/oauth/oauth.db`, mounted as
`/data/oauth/oauth.db` inside the container. Jenkins takes a transactionally
consistent pre-deploy snapshot and refuses to report deployment success if the
registered-client count decreases after replacement. Readiness also checks the
complete OAuth schema and SQLite integrity instead of accepting an empty or
partial database.

Google Cloud catalog access additionally requires `GCP_BILLING_API_KEY` in the
backend environment. AWS uses the existing signed AWS credentials; Azure Retail
Prices and OCI public list prices do not require account credentials.

Build and run:

```bash
docker compose -f deploy/compose.production.yml config --quiet
docker compose -f deploy/compose.production.yml build --pull
docker compose -f deploy/compose.production.yml up -d
```

Release checks must cover OAuth discovery, authenticated `initialize`,
`tools/list`, one official price query for each configured provider and one
successful `build_estimate` delivery.
