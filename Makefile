# Release consistency, and nothing else.
#
# This repository declares its version once, in the package itself. Two things are derived from that
# declaration by hand rather than by machinery, and either can be forgotten in a bump: the changelog
# section recording what shipped, and the install commands the documents hand a reader. Both defects
# are only visible after the tag is public, which is the point at which they are expensive — and the
# second is worse than the first, because a pin one release behind still resolves and still installs
# working code, so nothing reports it.
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
	echo "release-check: $$version — the declared version, $(CHANGELOG) and the $(DOCS)/ pins all agree"
