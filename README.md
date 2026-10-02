# Nani

Small Django app to manage our nanny's hours. Runs on the NAS as a Docker container, data in SQLite.
Works on the phone and can be added to the home screen. Vibecoded with Claude Opus 5.5.

## What it does

- **Weeks** – each day shows the plan, what was worked and the effect on the hour balance.
  The nanny only logs days that differ from the plan; past days without a log count as worked as planned.
  Evening and overnight engagements are added per day.
- **Vacation** – the nanny requests days, parents approve (email notifications both ways).
  Shows the allowance (25 days, 1/12 per full month in a partial year), days taken, requested and left.
- **Trips** – mileage log (date, purpose, km) reimbursed at 0.30 €/km.
- **Plan** (parents) – mark a range of days as "not needed" or with different times, and enter public holidays.
- **Month** (parents) – hours, balance, and what to pay: salary, paid evenings/overnights, paid-out hours, mileage.
  Balance adjustments (payout or correction) are entered here.

### Balance rules

| Day | Owed | Effect on balance |
|---|---|---|
| Regular workday (contract default Mon–Fri 15–19) | 4 h | worked − 4 h |
| Not needed (set by parents) | 4 h | −4 h, to be worked off later |
| Public holiday / approved vacation / sick | 0 h | none, unless she logs time |
| Weekend or other non-workday with logged time | 0 h | + worked |
| Evening, taken as time off | – | + its hours |
| Evening, paid | – | none; paid at 25 €/h |
| Overnight (always paid) | – | none; 25 €/h until 22:00 and from 06:00, plus 50 € |

More than 6 h in a day deducts a 30 min break automatically, unless a break is entered.
A day affects the balance once it is in the past, or as soon as times are logged for it.
All contract values (times, rates, allowance) are on the **Contract** in the admin.

## Login

There are no passwords. A user enters their email and gets a six-digit code (valid 10 minutes).
Only users that already exist can log in. Add them with:

```sh
python manage.py adduser you@example.com --name "You" --admin          # parent with admin access
python manage.py adduser partner@example.com --name "Partner"          # parent
python manage.py adduser nanny@example.com --name "Nanny" --role nanny
```

or via the Django admin at `/admin/`. If `EMAIL_HOST` is not set, emails are printed to the console / container logs.

## First-time setup

1. Create the users (above).
2. In the admin, add a **Contract**: pick the nanny and the start date. The other fields default to the
   signed contract (Mon–Fri 15:00–19:00, 2,166.67 €, 25 €/h, 50 € night flat rate, 0.30 €/km, 25 vacation days).
3. Enter the year's public holidays on the **Plan** tab.

## Local development

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
$env:DEBUG = "true"
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py adduser you@example.com --admin
.\.venv\Scripts\python manage.py runserver
```

Open http://127.0.0.1:8000, enter your email, and copy the code from the terminal output.

Run tests with `python manage.py test` (with `DEBUG=true`).

## Docker / NAS

```sh
cp .env.example .env   # set SECRET_KEY, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, SMTP...
docker compose up -d --build
```

- To try the image locally over http://localhost:8060 (separate database in `./data-local`;
  browsers refuse port 5060, which is only used behind the NAS reverse proxy):
  `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build`
- The SQLite database lives in `./data` (mounted at `/data`); back up that folder.
  The container runs as UID 1000, so that folder must be writable for it.
- Migrations run automatically on container start.
- `INITIAL_ADMIN_EMAIL` in `.env` creates the first admin user on startup.
- Health check: `GET /health/`.
- Run management commands with `docker compose exec nani python manage.py <command>`.

## Synology (Container Manager) via GitHub

Every push to `main` runs the tests and publishes the image `ghcr.io/<owner>/<repo>:latest`
(see `.github/workflows/docker.yml`). The NAS only pulls that image; all settings are
environment variables in the NAS project and never in git.

1. First time only: on GitHub → your profile → Packages → the package → Package settings →
   Change visibility → Public (so the NAS can pull without a token).
2. File Station: create the folders `/docker/nani` and `/docker/nani/data` (Synology doesn't create
   missing bind-mount folders).
3. Container Manager → Project → Create: name `nani`, path `/docker/nani`, source
   "Create docker-compose.yml", paste `deploy/docker-compose.nas.yml` and fill in the values.
4. DSM reverse proxy: `https://nani.bayley-plano.com:443` → `http://localhost:5060`, custom header
   `X-Forwarded-Proto: https`, Let's Encrypt certificate assigned.
5. Updates: push to `main`, wait for the GitHub Action, then in Container Manager → Project → nani:
   Stop → Build → Start. `pull_policy: always` in the compose file makes this fetch the new `latest`.
   `https://<host>/health/` shows the commit the running image was built from. Settings are changed in the same project
   (Edit the YAML), followed by a restart.
6. Back up `/docker/nani/data` (e.g. Hyper Backup).

## Layout

- `config/` – settings (all configured through environment variables), URLs, WSGI
- `accounts/` – email-based user model with parent/nanny roles, login codes, `adduser` command
- `schedule/` – contract, days, extras, vacation, trips, holidays; `engine.py` holds all balance/pay rules
- `core/` – health check, web app manifest, service worker
- `templates/`, `static/` – base template, CSS, icons
