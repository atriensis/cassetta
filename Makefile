# Release consistency, and nothing else.
#
# This repository states its version in two places and records what shipped in a third. Any one of
# them can be forgotten in a bump, and the resulting defect is only visible after the tag is public.
# `release-check` answers, in one command, whether the tree is internally consistent as a release.
#
# It performs no release: it creates no tag, builds nothing and uploads nothing.
#
# Deliberately shell rather than Python. A release is cut from a clean clone, which is exactly the
# state in which no virtual environment exists yet — so this must not need one. `sed` and `grep` are
# already assumed by scripts/smoke.sh.
#
# The suite holds the version-site half of this too (tests/test_release_metadata.py). Held twice on
# purpose: the test catches it during development, this catches it where a release is actually cut.

PYPROJECT    := pyproject.toml
PACKAGE_INIT := src/cassetta/__init__.py
CHANGELOG    := CHANGELOG.md

.PHONY: release-check

release-check:
	@packaging="$$(sed -n 's/^version = "\(.*\)"$$/\1/p' '$(PYPROJECT)' | head -n 1)"; \
	package="$$(sed -n 's/^__version__ = "\(.*\)"$$/\1/p' '$(PACKAGE_INIT)' | head -n 1)"; \
	if [ -z "$$packaging" ]; then \
		echo "release-check: no version declared in $(PYPROJECT)" >&2; \
		exit 1; \
	fi; \
	if [ -z "$$package" ]; then \
		echo "release-check: no __version__ declared in $(PACKAGE_INIT)" >&2; \
		exit 1; \
	fi; \
	if [ "$$packaging" != "$$package" ]; then \
		echo "release-check: the two version sites disagree." >&2; \
		echo "  $(PYPROJECT) declares    $$packaging" >&2; \
		echo "  $(PACKAGE_INIT) declares $$package" >&2; \
		exit 1; \
	fi; \
	if ! grep -q "^## \[$$packaging\]" '$(CHANGELOG)'; then \
		echo "release-check: $(CHANGELOG) has no section for $$packaging." >&2; \
		echo "  A release whose changelog has not caught up ships a document" >&2; \
		echo "  describing the version before it." >&2; \
		exit 1; \
	fi; \
	echo "release-check: $$packaging — both version sites agree and $(CHANGELOG) has its section"
