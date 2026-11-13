# Static case reader

Buildless HTML/CSS/JavaScript reader with local search, question cards, exact
quotations and full source-page images. It is served behind HTTPS and Basic Auth
on the owner's homelab. No third-party services or external assets are required
at runtime.

Only source code and the Humanizer editing instructions are versioned. Case
content, requirements, generated files, review reports and credentials remain
ignored. Do not copy the entire workspace into a release.

## Local build

The private source manifests and edited content must already exist in the
workspace. Editing must preserve quotations, numbers, negations, counterversions,
uncertainty and the mapping to source pages.

1. Run python3 site/prepare_sources.py to copy the allowlisted PDF packets and
   render one source image at a time with installed Poppler.
2. Prepare the ignored site/content/ui.json (presentation labels, person filters,
   source exclusions, expected source count and protected-path probes), then
   site/content/questions.edited.json and
   site/content/overview.edited.json, with their review files. The builder reads
   the canonical question/source manifests and validates quote and page links.
3. Run python3 site/build_site.py. Serve only site/dist, never the workspace
   root. Review desktop/mobile layouts, source navigation and local search.
4. After content review, run python3 site/build_site.py --package. This produces
   site/build/site.tar.zst and an artifact hash. The existing zstd executable
   runs with one worker.

release.json lists the relative path, bytes and SHA-256 of every release file
except itself. Archives contain regular files only. Deployment validates the
exact manifest, switches releases atomically and rolls back failed startup.

## Access and delivery

All HTML, JSON, search data, images and PDFs must pass through the same HTTPS
router and Basic Auth middleware. The static container has no publicly published
port. Do not publish a temporary unprotected route.

Site-only credentials are generated separately in the ignored private runtime
directory; the host receives only the password hash. Never print credentials,
pass them as shell arguments, or include them in Git, release files or logs.
When using htpasswd with stdin mode, provide one password line only. Verify the
generated database against that same single line locally before transferring
the hash file. Stdin mode does not use the interactive confirmation prompt.

After infrastructure is ready, run python3 site/verify_deployment.py to check
TLS, anonymous denial, authorized access and the exact deployed manifest.
site/send_credentials.py uses the installed Telegram bridge runtime; it accepts
--socket and --chat-id. Run it only after successful deployment verification
and explicit authorization to send the shared login to that recipient. It reads
the password in process memory, calls the normal bridge implementation and
checks confirmed history without printing the login message.

Keep site/build/state.json current for handoff. Stop the owned loopback preview
server after the public site is handed over.
