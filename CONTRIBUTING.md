# Contributing

Thank you for looking. Please read this before writing code — there is one requirement here that
will cost you your work if you meet it after the fact rather than before.

## The project is not looking for maintainers

This is stated plainly because the alternative is to let people find out slowly. Cassetta is a
single-author project with a deliberate scope, and it is not seeking co-maintainers, module owners,
or a governance structure. Issues, bug reports and small fixes are genuinely welcome. Large
contributions are likely to be declined on scope grounds even when they are good, and that is not a
judgement about the work.

If that makes forking the better option for you, the licence permits it — see
[docs/LICENSE_FAQ.md](docs/LICENSE_FAQ.md).

## The route

1. **Open an issue** describing the problem or the change. For a bug, say what you did, what you
   expected, and what happened.
2. **Wait for the discussion.** This is where scope gets settled, and it is the step that saves the
   most time. A change agreed here is a change that can be merged.
3. **Sign the contributor agreement** (see below) — before you write code, not after.
4. **Open a pull request.** Keep it to one subject. Include tests; the suite is the project's memory.
5. **Review and merge.** Pull requests are squash-merged, and the pull request body becomes the
   permanent commit message, so write it for someone reading the history in two years.

## The contributor agreement

**A code contribution is not reviewed until the agreement is signed. Without it, the pull request is
closed unread.**

That is blunt on purpose. The alternative — reading the code first and asking afterwards — means
that if you decline, the project has already seen your implementation of a problem it still needs to
solve, and cannot honestly write its own. Closing unread protects you as much as it protects the
project.

The agreement is [CLA.md](CLA.md), version 1.0. Read it, fill in the fields at the top, and email a
signed copy to **cassetta@atriensis.ai**, stating the version you signed. Ask before you start; it
takes one message.

A later version of the agreement binds only the people who sign that version — signing 1.0 keeps you
under 1.0.

### How the agreement differs from the Apache ICLA

The agreement is the Apache Software Foundation's Individual CLA, version 2.2, with four changes.
They are listed here rather than left to be found by diff.

1. **The counterparty is a person, not a foundation.** The Apache original names The Apache Software
   Foundation. Here it is the project's copyright holder, with an assignment clause so the agreement
   survives the project being placed into a company later. A grant to an entity that does not yet
   exist would be worth nothing.

2. **The nonprofit-purpose sentence is removed.** The Apache original promises that the Foundation
   will not use contributions in a way "contrary to the public benefit or inconsistent with its
   nonprofit status and bylaws". The maintainer is not a nonprofit and gives no such undertaking:
   this project is source-available under the FSL, it has a commercial counterpart, and the
   sublicensing right in section 2 is exercised knowingly. **This is the substantive difference, and
   it is stated here rather than left to be discovered.**

3. **A governing-law section is added.** The Apache original has none. Section 9 names German law,
   and says in the same breath that the choice does not deprive you of protections that cannot be
   waived under the law where you habitually live. There is deliberately no forum clause.

4. **Apache-specific mechanics are dropped**: the Apache id and notify-project fields, the
   public-profile note, the ASF privacy-policy reference and the postal address. Delivery is by email
   to the address above.

The grants themselves — sections 2 and 3, and the representations in 4 through 8 — are the Apache
text, altered only where it named the Foundation.

### Trivial changes are exempt

No agreement is needed for:

- a typo or grammatical fix,
- a one-line correction,
- a dead or wrong link.

If your change is a single obvious correction with no design content, open the pull request and say
so. This exemption is meant to be used — a project that demands paperwork for a misspelling gets the
misspelling instead.

The exemption covers the change, not the file: a "typo fix" that also renames a function or adjusts
behaviour is a code contribution.

## Before you open a pull request

Run what continuous integration runs:

```bash
uv run ruff format --check
uv run ruff check
uv run mypy
uv run pytest
uv run pytest -p randomly
./scripts/smoke.sh
```

The randomised-order run is not optional. Every test must pass in isolation and under any ordering;
a test that depends on state another test left behind is a defect in this project, not a quirk.

`./scripts/smoke.sh` checks a change end to end against a real container: it builds the image,
brings the stack up and walks the quickstart over HTTP. Continuous integration runs it on every
pull request. Running it locally takes Docker with Compose v2, curl, openssl and python3.

## What lands in the changelog

Anything a running deployment or a dependent project would notice: a configuration variable, a REST
route, an MCP tool, a structured-log field, or the shape of a response. If your change touches one
of those, say so in operator-facing terms in the pull request body — that text is what becomes the
entry in [CHANGELOG.md](CHANGELOG.md).

## Security

Do not open an issue for a vulnerability. See [SECURITY.md](SECURITY.md) for the private channel.
