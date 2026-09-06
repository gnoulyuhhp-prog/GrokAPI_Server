# VPS deployment (`grokapi.vorte.me`)

This stack packages a prebuilt custom Sub2API binary on the upstream runtime
image. It exposes only Caddy on ports 80/443; PostgreSQL, Redis and Sub2API stay
on private Docker networks.

## DNS and firewall

Create an `A` record for `grokapi.vorte.me` pointing to `103.238.235.241`.
Remove any `AAAA` record for this host unless the VPS has working public IPv6.
Allow inbound TCP 22, 80 and 443 (and UDP 443 for HTTP/3). Do not expose 5432,
6379 or 8080.

## First deployment

Run from the repository root on the VPS:

```sh
cd deploy/vps
cp .env.example .env
chmod 600 .env
```

Replace every `replace-with-...` value. Generate independent random values,
for example with `openssl rand -hex 32`. Use a real mailbox for `ADMIN_EMAIL`.
Then validate and start the stack:

```sh
test -x ./sub2api
docker compose config --quiet
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=100 sub2api caddy
```

Open `https://grokapi.vorte.me` after DNS propagation and Caddy has obtained a
certificate. `ADMIN_EMAIL` and `ADMIN_PASSWORD` are used only when the database
is initialized for the first time.

## Updates

```sh
git pull --ff-only
# Rebuild deploy/vps/sub2api from the updated frontend/backend source first.
cd deploy/vps
docker compose build
docker compose up -d
```

## Database backup and restore

Named volumes persist across container replacement. Keep an off-VPS PostgreSQL
backup as well:

```sh
mkdir -p backups
docker compose exec -T postgres pg_dump -U sub2api -d sub2api -Fc > "backups/sub2api-$(date +%F-%H%M%S).dump"
```

Restore into an empty database during a maintenance window:

```sh
docker compose stop sub2api
docker compose exec -T postgres pg_restore -U sub2api -d sub2api --clean --if-exists < backups/sub2api.dump
docker compose start sub2api
```

Back up `.env` separately in a secure password manager. Losing `JWT_SECRET` or
`TOTP_ENCRYPTION_KEY` invalidates sessions or configured two-factor secrets.
