# Private Compose images on GHCR

The fork uses three separate GitHub Container Registry packages under
`ghcr.io/olaclaw`: `nanoclaw-host`, `nanoclaw-agent` and `nanoclaw-brokers`.
The `publish-compose` CI job builds all three from the same `main` commit,
repeats the source, context and image-layer privacy gates, then pushes only
commit-SHA tags. It generates a six-image, digest-pinned `compose-release.json`
artifact from the published fork images and the external image pins in
`.env.example`. No `latest` tag is published. This job never runs for a pull
request or for another repository. It checks the GitHub Packages API before
any push and after each push: existing public packages or an unreadable
visibility result block the job, as does a newly created package that is not
private.

The job runs only when an operator manually dispatches **CI** on `main` with
the **Publish private Compose images** input checked. A normal push, a pull
request, or a manual run with the input unchecked cannot publish. Before the
first release, the organization owner must check **olaClaw → Settings →
Packages → Package Creation** in GitHub's browser interface and restrict
creation to private packages. GitHub says new GHCR packages start private and
a linked public repository does not change their visibility, but a public
package cannot be made private again. No repository variable or personal
access token is needed: the job uses its short-lived `GITHUB_TOKEN` with
`packages: write` after all required jobs have succeeded. The source label
links each package to this repository for workflow access; it does not make
the package public.

To start a release in the browser, open **Actions → CI → Run workflow**, select
`main`, check **Publish private Compose images**, and run it. From an authenticated
GitHub CLI, the equivalent is
`gh workflow run ci.yml --repo olaClaw/nanoclaw --ref main -f publish_compose=true`.
Check that the run uses the intended `main` commit before using its artifact.
The manual input is per run; no later push is armed by this release.

After the successful manual CI run, inspect **olaClaw → Packages**
and verify that all three packages say **Private**. Download the
`compose-release-<commit>` artifact from that run. Its `revision` and `tree`
must match the checked-out release commit, and its three fork references must
be `ghcr.io/olaclaw/...@sha256:...`. Keep the manifest with the private runtime
configuration; the host checks the manifest and every image before startup.
Do not run a deployment from a failed CI run or from tag-only references.

The target Docker host needs its own read-only GHCR credential to pull private
packages. Create and store that credential on the target outside Git and CI;
never place it in `.env`, Compose YAML, images or the release manifest. Test
pulls by digest with a disposable account before installing actual runtime
secrets. The Docker LXC still needs its own bootstrap, permissions, isolation,
agent-session and recovery rehearsal with test identities.

GitHub references: [Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry),
[package access and visibility](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility),
and [publishing images from Actions](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images).
