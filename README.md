# Chef

Small Django app to plan our family's meals for the week and share the shopping list.
Runs on the NAS as a Docker container, data in SQLite. Works on the phone and can be added
to the home screen. Vibecoded with Claude Opus 5.5.

## What it does

- **Home** – today's meals and the rest of the week, with one-tap feedback.
- **Menu** – the week's meals as cards (recipe link, cooking time, portions, notes). Meals can be added,
  changed or removed by hand, or the whole week can be planned with AI (**✨ Create menu**).
- **Shopping** – the week's list, built from the menu's ingredients: merged across recipes, scaled to the
  portions planned, grouped by section, with the meals each item is for. Ticks sync between phones.
- **Family** – family members (likes, dislikes, allergies), the usual week (which meals, who eats),
  planning rules and household settings. All of it, plus past menus and feedback, goes into AI planning.

## AI menu planning

**✨ Create menu** on the week page asks which meals are needed and who eats them (prefilled from the
usual week) and for notes about the week. An OpenAI model (`AI_MODEL`, default `gpt-6.1-sol`) then plans
the meals in the background through the Responses API, searches the web for recipes and returns them
through a strict function schema; they are saved as normal dishes, ingredients and meals, so the
shopping list follows. See `meals/planner.py`.

- Needs `OPENAI_API_KEY` (from platform.openai.com; API use is billed separately from a ChatGPT
  subscription). Without it the feature is switched off.
- `AI_MODEL=gpt-6-astra` is OpenAI's flagship for complex reasoning (about 5x the price of `gpt-6.1-sol`).
  `AI_EFFORT` sets the reasoning effort (low, medium, high, xhigh).
- Token counts per request are in the admin (Menu requests).

## Login

There are no passwords. A user enters their email and gets a six-digit code (valid 10 minutes).
Only users that already exist can log in. Add them with:

```sh
python manage.py adduser you@example.com --name "You" --admin    # with admin access
python manage.py adduser partner@example.com --name "Partner"
```

or via the Django admin at `/admin/`. If `EMAIL_HOST` is not set, emails are printed to the console / container logs.

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

- To try the image locally over http://localhost:8061 (separate database in `./data-local`;
  browsers refuse port 5061, which is only used behind the NAS reverse proxy):
  `docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build`
- The SQLite database lives in `./data` (mounted at `/data`); back up that folder.
  The container runs as UID 1000, so that folder must be writable for it.
- Migrations run automatically on container start.
- `INITIAL_ADMIN_EMAIL` in `.env` creates the first admin user on startup.
- Health check: `GET /health/`.
- Run management commands with `docker compose exec chef python manage.py <command>`.

## Synology (Container Manager) via GitHub

Every push to `main` runs the tests and publishes the image `ghcr.io/<owner>/<repo>:latest`
(see `.github/workflows/docker.yml`). The NAS only pulls that image; all settings are
environment variables in the NAS project and never in git.

1. First time only: on GitHub → your profile → Packages → the package → Package settings →
   Change visibility → Public (so the NAS can pull without a token).
2. File Station: create the folders `/docker/chef` and `/docker/chef/data` (Synology doesn't create
   missing bind-mount folders).
3. Container Manager → Project → Create: name `chef`, path `/docker/chef`, source
   "Create docker-compose.yml", paste `deploy/docker-compose.nas.yml` and fill in the values.
4. DSM reverse proxy: `https://chef.example.com:443` → `http://localhost:5061`, custom header
   `X-Forwarded-Proto: https`, Let's Encrypt certificate assigned.
5. Updates: push to `main`, wait for the GitHub Action, then in Container Manager → Project → chef:
   Stop → Build → Start. `pull_policy: always` in the compose file makes this fetch the new `latest`.
   `https://<host>/health/` shows the commit the running image was built from. Settings are changed in the same project
   (Edit the YAML), followed by a restart.
6. Back up `/docker/chef/data` (e.g. Hyper Backup).

## Layout

- `config/` – settings (all configured through environment variables), URLs, WSGI
- `accounts/` – email-based user model, login codes, `adduser` command
- `meals/` – weekly menu and shopping list
- `core/` – health check, web app manifest, service worker
- `templates/`, `static/` – base template, CSS, icons
