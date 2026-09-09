# Release consistency, and nothing else.
#
# This repository declares its version once, in the package itself. Three things are derived from
# that declaration, and any of them can be forgotten in a bump: the changelog section recording what
# shipped, the install commands the documents hand a reader, and the sample of what
# `cassetta capabilities` prints. All three defects are only visible after the tag is public, which
# is the point at which they are expensive — and the last two are worse than the first, because a pin
# one release behind still resolves and still installs working code, and a stale sample output is
# just a number in a code block. Nothing reports either.
#
# The changelog is still written by hand. The other two have machinery —
# scripts/sync-docs-version.py rewrites both — so what this target checks for them is that somebody
# ran it.
#
# `release-check` answers, in one command, whether the tree is internally consistent as a release.
#
# It performs no release: it creates no tag, builds nothing and uploads nothing. The tag is placed by
# .github/workflows/tag.yml after the merge; this is what you run before opening the pull request.
#
# Deliberately shell rather than Python. A release is cut from a clean clone, which is exactly the
# state in which no virtual environment exists yet — so this must not need one, and must not reach
# for `uv`. `sed` and `grep` are already assumed by scripts/smoke.sh.
#
# The suite holds these same invariants (tests/test_release_metadata.py, tests/test_docs_version_pins.py).
# Held twice on purpose: the tests catch it during development, this catches it where a release is
# actually cut.

PACKAGE_INIT := src/cassetta/__init__.py
CHANGELOG    := CHANGELOG.md
DOCS         := docs

.PHONY: release-check

release-check:
	@version="$$(sed -n 's/^__version__ = "\(.*\)"$$/\1/p' '$(PACKAGE_INIT)' | head -n 1)"; \
	if [ -z "$$version" ]; then \
		echo "release-check: no __version__ declared in $(PACKAGE_INIT)" >&2; \
		exit 1; \
	fi; \
	if ! grep -q "^## \[$$version\]" '$(CHANGELOG)'; then \
		echo "release-check: $(CHANGELOG) has no section for $$version." >&2; \
		echo "  A release whose changelog has not caught up ships a document" >&2; \
		echo "  describing the version before it." >&2; \
		exit 1; \
	fi; \
	pins="$$(grep -rhoE 'cassetta\.git@v[0-9]+\.[0-9]+\.[0-9]+' '$(DOCS)' | sed 's/.*@v//' | sort -u)"; \
	if [ -z "$$pins" ]; then \
		echo "release-check: no documented install pin found under $(DOCS)/." >&2; \
		echo "  Nothing to compare means nothing to disagree — which is not the same" >&2; \
		echo "  as agreement. Either the install instructions have gone, or this" >&2; \
		echo "  check no longer recognises them." >&2; \
		exit 1; \
	fi; \
	stale="$$(printf '%s\n' "$$pins" | grep -vx "$$version" || true)"; \
	if [ -n "$$stale" ]; then \
		echo "release-check: the documented install commands pin another release." >&2; \
		echo "  $(PACKAGE_INIT) declares  $$version" >&2; \
		echo "  $(DOCS)/ pins             $$(printf '%s' "$$stale" | tr '\n' ' ')" >&2; \
		echo "  A stale pin installs working code, so nobody finds out." >&2; \
		echo "  Fix with: python scripts/sync-docs-version.py" >&2; \
		exit 1; \
	fi; \
	samples="$$(grep -rhoE 'Server version: *[0-9]+\.[0-9]+\.[0-9]+' '$(DOCS)' | sed 's/.*: *//' | sort -u)"; \
	if [ -z "$$samples" ]; then \
		echo "release-check: no documented sample output found under $(DOCS)/." >&2; \
		echo "  \`cassetta capabilities\` prints \"Server version: X.Y.Z\", and the" >&2; \
		echo "  documented sample of it is the one version literal no install-pin" >&2; \
		echo "  check can see. Nothing to compare is not agreement." >&2; \
		exit 1; \
	fi; \
	stale="$$(printf '%s\n' "$$samples" | grep -vx "$$version" || true)"; \
	if [ -n "$$stale" ]; then \
		echo "release-check: a documented sample output names another release." >&2; \
		echo "  $(PACKAGE_INIT) declares  $$version" >&2; \
		echo "  $(DOCS)/ samples print    $$(printf '%s' "$$stale" | tr '\n' ' ')" >&2; \
		echo "  Nothing else looks at this shape, so it goes stale in silence." >&2; \
		echo "  Fix with: python scripts/sync-docs-version.py" >&2; \
		exit 1; \
	fi; \
	echo "release-check: $$version — the declared version, $(CHANGELOG), the $(DOCS)/ install pins and the $(DOCS)/ sample output all agree"
