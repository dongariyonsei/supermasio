# Supermasio

FastAPI + Fly.io deployment setup for the restaurant ordering app.

## CI/CD

- `pull_request`: runs tests only.
- `push` to `main`: runs tests only.
- `GitHub Release published`: runs tests and then deploys to Fly.

## Fly token setup

Use a Fly deploy token for GitHub Actions.

Recommended:

```bash
fly tokens create deploy --app supermasio --name "github-actions" --expiry 90d
```

Then add the token to GitHub repository secrets as:

- `FLY_API_TOKEN`

You can also create or revoke tokens from the Fly dashboard:
- App page > Tokens

Do not use the short-lived `fly auth token` output for GitHub Actions.

## Release-based deploy flow

1. Merge your changes to `main`.
2. Validate CI on the branch or pull request.
3. Create a GitHub Release from the commit you want to deploy.
4. The release publication triggers the Fly deploy workflow.
