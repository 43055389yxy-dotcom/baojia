# AstraQuote production deployment

- Public address: `https://baojia.tontiancloud.com`
- Container: `astraquote`
- Runtime data: `/home/ec2-user/astraquote/data`
- Local secrets: `/home/ec2-user/astraquote/config/backend.env`
- Caddy route: `/home/ec2-user/caddy-gateway/managed/astraquote.caddy`

The repository does not contain credentials, customer confirmation sessions, or historical quotes.
The Docker image contains only a compressed AWS public catalog seed. Runtime cache and sessions
are stored in the host data directory so a rebuild does not erase active work.
