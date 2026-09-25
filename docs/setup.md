# Set up a software factory

This guide creates an instance of package `coder`, logs in its coding clients, and starts it. The instance stays stopped until the last step, so no routine claims an issue before setup is done.

Two routes lead to the same instance: through a kinby hub, or by hand with Docker. Both use an image built from kinby's base image plus [`image/recipe.Dockerfile`](../image/recipe.Dockerfile), with this package installed at one commit.

The recipe adds pinned versions of gh, Claude Code, Codex and Bun to kinby's base image, which already has git, Python and uv. Bun is there for repositories with a TypeScript side, so the checks in `package.yaml` can run `bun` commands.

## What you need

- A GitHub repository for the factory to work in, and a token with `repo` scope for it.
- A Claude subscription for Claude Code, the default implementer.
- A ChatGPT subscription for Codex, if you switch babysitting on or pick Codex as the implementer.
- A random string for the webhook secret: `openssl rand -hex 32`.

The instance's `.env` holds:

| Variable | What it is |
| --- | --- |
| `GH_TOKEN` | The GitHub token. gh, git push, issues and pull requests run as it. Required. |
| `GITHUB_WEBHOOK_SECRET` | The shared secret of the repository webhook. Required. |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code's subscription token from `claude setup-token`. |
| `GIT_USER_NAME`, `GIT_USER_EMAIL` | The commit identity inside the container. |
| `ANTHROPIC_API_KEY` | The instance's own model key. The factory keeps it away from Claude Code, which would otherwise bill it over the subscription. |

## Through a hub

1. **Create the instance stopped.** Create it from package `coder` with the distribution `kinby-code-factory`, the git source `https://github.com/jorgesolerrr/kinby-code-factory` at a full commit SHA of `main`, and the recipe file's text as the image recipe. [`catalog/coder.json`](../catalog/coder.json) holds these values. Enter the secrets from the table. The hub builds the image, checks the package inside it, and leaves the instance stopped.

2. **Configure it.** In the instance directory, set `[workspace].source` in `kinby.toml` to your repository, and replace the check commands in `package.yaml` with the ones your repository runs. Every other setting in `package.yaml` has a comment saying what it does.

3. **Log in.** Run a temporary setup container from the instance's image. It overrides the entrypoint, so kinby, its scheduler and its routines never start:

   ```sh
   docker run --rm -it --entrypoint claude <image-id> setup-token
   ```

   Enter the printed token as the instance's `CLAUDE_CODE_OAUTH_TOKEN` secret. For Codex, mount the instance's Codex volume so the login outlives the container:

   ```sh
   docker run --rm -it --entrypoint codex \
     --mount type=volume,src=kinby-<instance-id>-codex,dst=/root/.codex \
     <image-id> login --device-auth
   ```

   Open the URL it prints and enter the one-time code. A container update keeps the volume, so the login survives it.

4. **Register the webhook.** Point one repository webhook at each routine, both signed with `GITHUB_WEBHOOK_SECRET`:

   | URL | Events |
   | --- | --- |
   | `https://<hub-domain>/instances/<instance-id>/signals/implement-ready-issue` | `issues`, `pull_request` |
   | `https://<hub-domain>/instances/<instance-id>/signals/babysit-pull-request` | `pull_request_review`, `pull_request_review_comment`, `issue_comment` |

   Babysitting labels pull requests `merge-ready`. Create the label once: `gh label create merge-ready --color 0E8A16`.

5. **Start it.** Start the instance from the hub. Each routine also scans hourly, so a missed delivery is picked up within the hour.

## By hand, without a hub

Build the image the way the hub does, from a kinby checkout and one commit of this repository:

```sh
scripts/build-image.sh ../kinby <factory-commit-sha> kinby-coder
```

Initialize the instance. `init` refuses a directory that is not empty, and a failed initialization leaves nothing behind:

```sh
mkdir coder
docker run --rm --mount type=bind,src="$PWD/coder",dst=/instance \
  kinby-coder init /instance --package coder --model anthropic:claude-sonnet-5
```

Configure it as in step 2 above, and write the variables from the table to `coder/.env`. Log in as in step 3, with a named volume of your own for Codex, such as `coder-codex`. Then run the hub's candidate check, which validates your `package.yaml` in the image and prints the package on success, and start the instance:

```sh
docker run --rm --mount type=bind,src="$PWD/coder",dst=/instance \
  --entrypoint python kinby-coder -m kinby.packages coder /instance
docker run --detach --name coder --env-file coder/.env -p 127.0.0.1:8787:8787 \
  --mount type=bind,src="$PWD/coder",dst=/instance \
  --mount type=volume,src=coder-workspace,dst=/instance/workspace \
  --mount type=volume,src=coder-codex,dst=/root/.codex \
  kinby-coder serve
```

GitHub cannot reach `localhost`. Forward deliveries with the gh webhook extension (`gh extension install cli/gh-webhook`) while you test:

```sh
gh webhook forward --repo <owner>/<repository> --events issues,pull_request \
  --url http://localhost:8787/signals/implement-ready-issue --secret "$GITHUB_WEBHOOK_SECRET"
```

## Check that it works

Label a small issue `ready-for-agent`. The factory branches from the default branch, implements it, runs the configured checks, and opens a pull request that closes the issue. A failure removes the label, adds `ready-for-human`, and comments the reason on the issue.

A missing `GH_TOKEN` fails the run with that name in the error. A Claude Code login that expired fails the issue's run and labels it `ready-for-human`. Neither reports "no work".
