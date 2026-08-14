# Contributing to Aegis

Thanks for your interest in improving Aegis! Contributions happen through
**forks and pull requests** — please don't push branches directly to the main
repository.

## Workflow

1. **Fork** the repository to your own account (use the *Fork* button on GitHub).
2. **Clone your fork** and add the upstream remote:
   ```bash
   git clone https://github.com/<your-username>/aegis.git
   cd aegis
   git remote add upstream https://github.com/dhpradeep/aegis.git
   ```
3. **Create a branch** off `main` for your change:
   ```bash
   git checkout -b fix/short-description
   ```
4. **Make your change**, keeping commits focused and messages descriptive.
5. **Run the tests** (see below) and make sure they pass.
6. **Push** to your fork and **open a pull request** against `upstream/main`.
   Describe what the change does and why; link any related issue.

Keep your fork current by pulling from upstream before starting new work:
```bash
git fetch upstream && git rebase upstream/main
```

## Development setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run claude auth login      # one-time Claude subscription sign-in
uv run aegis                  # start the server (http://localhost:8000/admin)
```

## Tests

Every change should keep the suite green, and new behavior should come with
tests:

```bash
uv run pytest
```

The suite uses ephemeral SQLite DBs and needs no network or Claude login.

## Database changes

If you change a model in `app/db/models/`, generate a migration and commit it:

```bash
uv run alembic revision --autogenerate -m "describe the change"
```

Review the generated file before committing — autogenerate is a starting point,
not the final word.

## Guidelines

- Follow the existing layered structure (`core` / `db` / `schemas` / `services` / `api`).
- Keep functions and files focused; match the style of the code around you.
- Never introduce an `ANTHROPIC_API_KEY` path into the agent runtime — Aegis
  authenticates strictly via the Claude subscription login.
- Open an issue first for large or breaking changes so we can align on direction.

By contributing, you agree that your contributions are licensed under the
project's [Apache 2.0 License](LICENSE).

## Releasing

Maintainers cut a release by pushing a semver tag:

```bash
git tag v1.1.0 && git push origin v1.1.0
```

The `release` workflow then runs the test suite, builds and pushes a
multi-arch (amd64 + arm64) image to Docker Hub as
`dhpradeep/aegis:<version>`, `<major>.<minor>`, and `latest`, and creates a
GitHub Release with generated notes. It needs two repo secrets:
`DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a Docker Hub access token).
