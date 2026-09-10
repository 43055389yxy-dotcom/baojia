# AstraQuote sales portal

The frontend exposes two equivalent entry routes:

- `/sales`
- `/`

Both routes provide the production sales quote form and status view. Internal
model, prompt, cleaning, tool-call, and browser-automation details are never
rendered to sales users.

## Commands

```bash
npm ci
npm test
npm run build
```

Production is built by `deploy/Dockerfile` and served by the single AstraQuote
container described in `deploy/compose.production.yml`.
