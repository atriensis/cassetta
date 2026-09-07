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

The agreement is not in this repository. It is provided on request: email **cassetta@atriensis.ai**
and it will be sent to you. Ask before you start; it takes one message.

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
```

The randomised-order run is not optional. Every test must pass in isolation and under any ordering;
a test that depends on state another test left behind is a defect in this project, not a quirk.

To check a change end to end against a real container, `scripts/smoke.sh` builds the image, brings
the stack up and walks the quickstart over HTTP.

## What lands in the changelog

Anything a running deployment or a dependent project would notice: a configuration variable, a REST
route, an MCP tool, a structured-log field, or the shape of a response. If your change touches one
of those, say so in operator-facing terms in the pull request body — that text is what becomes the
entry in [CHANGELOG.md](CHANGELOG.md).

## Security

Do not open an issue for a vulnerability. See [SECURITY.md](SECURITY.md) for the private channel.
