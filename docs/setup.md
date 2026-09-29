# Set up a software factory

This guide creates an instance of package `coder`, signs in its coding clients, and starts it. The instance stays stopped until the last step, so no routine claims an issue before setup is done.

The usual route is kinby's web app, which asks for everything in one wizard. Building and running the container with Docker by hand is the fallback. Both use an image built from kinby's base image plus [`image/recipe.Dockerfile`](../image/recipe.Dockerfile), with this package installed at one commit.

The recipe adds pinned versions of gh, Claude Code, Codex and Bun to kinby's base image, which already has git, Python and uv. Bun is there for repositories with a TypeScript side, so the checks in `package.yaml` can run `bun` commands.

## What you need

- A GitHub repository for the factory to work in, and a token with `repo` scope for it.
- A Claude subscription for Claude Code, the default implementer, and Claude Code on your own machine to create its token.
- A ChatGPT subscription for Codex, if you switch babysitting on or pick Codex as the implementer.
- A random string for the webhook secret: `openssl rand -hex 32`.

## From the web app

1. **Create the instance.** In the web app, create an instance and pick **Software factory** from kinby's package list. The wizard asks for:

   | Field | What it is |
   | --- | --- |
   | Model | The instance's own model. It defaults to `anthropic:claude-sonnet-5`. |
   | API key | The key for that model. The factory keeps it away from Claude Code, which would otherwise bill it over the subscription. |
   | Repository | The git URL of the repository to work in. It lands in `kinby.toml` as `[workspace].source`. |
   | Commit author name and email | The identity on the factory's commits. It lands in `package.yaml` under `commit`. |
   | `GH_TOKEN` | The GitHub token. gh, git push, issues and pull requests run as it. |
   | `GITHUB_WEBHOOK_SECRET` | The shared secret of the repository webhook. |
   | `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code's subscription token. Run `claude setup-token` on your own machine and paste what it prints. The app cannot relay the code that command asks for, so it runs outside the app. |

   The hub prepares the image, checks the package inside it, and creates the instance stopped.

2. **Sign in to Codex.** The wizard's Sign in step has a Codex row. Start it, open the URL it shows, and enter the one-time code. The login lands on the instance's Codex volume, so it survives container updates. The code expires after 15 minutes; start the row again for a new one.

3. **Set the checks.** In the instance directory, replace the check commands in `package.yaml` with the ones your repository runs. Every other setting in `package.yaml` has a comment saying what it does.

4. **Register the webhook.** Point one repository webhook at each routine, both signed with `GITHUB_WEBHOOK_SECRET`:

   | URL | Events |
   | --- | --- |
   | `https://<hub-domain>/instances/<instance-id>/signals/implement-ready-issue` | `issues`, `pull_request` |
   | `https://<hub-domain>/instances/<instance-id>/signals/babysit-pull-request` | `pull_request_review`, `pull_request_review_comment`, `issue_comment` |

   Babysitting labels pull requests `merge-ready`. Create the label once: `gh label create merge-ready --color 0E8A16`.

5. **Start it.** Start the instance from the web app. Each routine also scans hourly, so a missed delivery is picked up within the hour.

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

Configure it by hand: set `[workspace].source` in `coder/kinby.toml` to your repository, add a `commit` section with `name` and `email` to `coder/package.yaml`, and set the checks as in step 3 above. Write `GH_TOKEN`, `GITHUB_WEBHOOK_SECRET`, `CLAUDE_CODE_OAUTH_TOKEN` and `ANTHROPIC_API_KEY` to `coder/.env`. Create the Claude Code token with `claude setup-token` on your own machine, and sign in to Codex in a setup container that overrides the entrypoint, so kinby never starts, with a named volume of your own:

```sh
docker run --rm -it --entrypoint codex \
  --mount type=volume,src=coder-codex,dst=/root/.codex \
  kinby-coder login --device-auth
```

Open the URL it prints and enter the one-time code. Then run the hub's candidate check, which validates your `package.yaml` in the image and prints the package on success, and start the instance:

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

Label a small issue `ready-for-agent`. The factory branches from the default branch, implements it, runs the configured checks, and opens a pull request that closes the issue. In a repository a user owns, the pull request requests that user's review. GitHub cannot ask an organization for a review, so in an organization's repository it requests none. A failure removes the label, adds `ready-for-human`, and comments the reason on the issue.

A missing `GH_TOKEN` fails the run with that name in the error. A Claude Code login that expired fails the issue's run and labels it `ready-for-human`. Neither reports "no work".
