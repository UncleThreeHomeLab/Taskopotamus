# Agent instructions

## Project goal

Taskopotamus is a self-hosted AI agent built with the Codex SDK and a Telegram bot interface.
Support deployment with Docker Compose and a Helm chart for Kubernetes.
Use Docker Compose for local development and integration testing where practical.

## Repository structure and readability

- Before adding or moving files, choose descriptive filenames and a logical folder structure that make each file's purpose easy to understand.
- Group related code by responsibility. Keep application code, tests, deployment configuration, and documentation in predictable locations.
- Follow Python naming conventions and use consistent names across code, tests, and documentation. Avoid vague names such as `misc`, `stuff`, or catch-all `utils` modules.
- Keep the repository root tidy and the folder hierarchy shallow. Add folders when they improve navigation, not for hypothetical future features.
- Prefer small, focused modules and straightforward code. Use comments to explain non-obvious decisions rather than restating the code.
- When changing the structure, update imports, Docker paths, tests, and relevant documentation together so the repository remains easy to run and navigate. Follow the README restriction below.

## Documentation

- Do not create additional documentation, reports, or Markdown files unless the user explicitly requests them. Keep review findings in the chat by default.
- Do not edit `README.md` unless the user explicitly requests a README change. Code, configuration, or folder changes do not implicitly authorize README updates.

## Testing

- Keep the default test suite offline. Use synthetic data and mocked service clients; never call live Telegram, Codex, OpenAI, or other external services from automated tests.
- Default test commands must not require real credentials or consume paid service usage.
- Do not perform live testing through Python scripts, ad hoc Python snippets, or other scripted test calls.
- When live verification is needed, test manually through the application's normal interface, such as the Telegram bot. Keep live verification separate from the default test suite.
- Report automated test results and manual live verification separately. If no manual live verification was performed, say so.

## Commit messages and pull request titles

- Use Conventional Commit format for commit subjects and pull request titles: `type(scope): summary`. Omit `(scope)` when it adds no useful context.
- Choose the type that matches the change: `feat` for new behavior, `fix` for a bug fix, `refactor` for restructuring without behavior changes, `docs` for documentation, `test` for tests, `build` for dependencies or build configuration, `ci` for CI workflows, and `chore` for other maintenance.
- Use a short, lowercase scope that identifies the affected area, such as `telegram`, `agent`, or `deploy`. Omit the scope for changes spanning several areas.
- Write the summary in imperative form, such as `add conversation reset`, rather than `added conversation reset`. Do not end it with a period.
- Aim for 72 characters or fewer for the complete subject or title. Prefer a clear description over an unclear abbreviation.
- Describe the concrete change. Avoid vague subjects such as `update files`, `fix stuff`, or `WIP`, and omit emojis and conversation history.
- Use `!` before the colon only for a breaking change, such as `feat(agent)!: change chat request format`. Explain the impact and required migration in the commit body when needed.
- Keep commit bodies optional. When useful, explain why the change was made and any important limitations; do not repeat the diff.
- Write the pull request title around the final combined change, even when the branch contains several commits. Keep the pull request description limited to the four-section template below.

## Pull request descriptions

Use only the following template for every pull request description, with exactly these four sections in this order. Do not add other sections, preambles, footers, checklists, or text outside the template. Keep each section concise and specific to the final changes. Write in normal English and distinguish intended outcomes from verified results.

```markdown
## Objective

What this change aims to accomplish.

## Motivation

Why this change is needed and what problem it solves.

## Risk

What could go wrong, who or what could be affected, and how the risk is reduced. State when no material risks are identified.

## Intended result

The behavior or outcome this change should achieve. Include relevant validation results, clearly separating automated tests from manual live verification and noting anything not verified.
```

## Security and commits

- Never commit real secrets: Telegram bot tokens, API keys, Codex credentials, session files, private keys, or Kubernetes credentials.
- Keep local `.env` files, credential directories, logs, conversation history, and runtime data out of Git. Add appropriate ignore rules before introducing these files.
- Commit only placeholder values in example environment files and Helm values. Kubernetes Secret manifests must not contain real credentials; base64 encoding is not encryption.
- Supply secrets at runtime through environment variables, mounted secret files, or references to existing Kubernetes Secrets. Never bake secrets into container images or build arguments.
- Do not print secrets in logs, test output, screenshots, commit messages, or pull request descriptions. Use synthetic data in tests.
- Before committing, review `git status --short`, `git diff --cached`, and `git diff --cached --check`. Inspect every staged file, including new files, for secrets and private data. Use an available secret scanner as an additional check.
- Stage only files relevant to the task. Do not commit unrelated user changes or generated runtime artifacts.
- If a secret is exposed, stop sharing it, notify the user without repeating it, and arrange revocation or rotation. Deleting it from the latest file does not remove it from Git history. Do not rewrite shared history without explicit authorization.
