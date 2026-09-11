# Release consistency, and nothing else.
#
# This repository declares its version once, in the package itself. Two things are derived from that
# declaration, and either can be forgotten in a bump: the changelog section recording what shipped,
# and the sample of what `cassetta capabilities` prints. Both defects are only visible after the tag
# is public, which is the point at which they are expensive — and the second is worse than the first,
# because a stale sample output is just a number in a code block. Nothing reports it.
#
# There used to be a third: the install commands the documents hand a reader, pinned to a release
# tag. They install from the package index now, with no version at all, so no pin is left to fall
# behind, and tests/test_docs_install.py holds that none comes back. A branch here still comparing
# pins would find nothing, and would refuse every release on the absence.
#
# The changelog is still written by hand. The sample output has machinery —
# scripts/sync-docs-version.py rewrites it — so what this target checks for it is that somebody ran it.
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
	samples="$$(grep -rhoE 'Server version: *[0-9]+\.[0-9]+\.[0-9]+' '$(DOCS)' | sed 's/.*: *//' | sort -u)"; \
	if [ -z "$$samples" ]; then \
		echo "release-check: no documented sample output found under $(DOCS)/." >&2; \
		echo "  \`cassetta capabilities\` prints \"Server version: X.Y.Z\", and the" >&2; \
		echo "  documented sample of it is the one version literal the documents" >&2; \
		echo "  carry. Nothing to compare is not agreement." >&2; \
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
	echo "release-check: $$version — the declared version, $(CHANGELOG) and the $(DOCS)/ sample output all agree"
