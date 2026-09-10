# AstraQuote OAuth gateway

The gateway is packaged inside the single AstraQuote production container. It
publishes `https://pricing-mcp.tontiancloud.com/mcp` and proxies authenticated
requests to the loopback-only AstraQuote MCP process.

The public MCP exposes exactly four tools:

1. `describe_service`
2. `get_attribute_values`
3. `get_prices`
4. `build_estimate`

The first three tools require `pricing:read`; `build_estimate` requires
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
