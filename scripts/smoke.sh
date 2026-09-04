#!/usr/bin/env bash
#
# End-to-end smoke test for a real deployment of Cassetta.
#
# It walks the README quickstart literally — prepare the environment file, start the stack, mint
# a key, store a file, read it back — against a container built from this tree. That coupling is
# the point: if the documented steps drift from what actually works, this goes red.
#
# Usage, from a clean clone of this repository:
#
#     ./scripts/smoke.sh
#
# Requires Docker with Compose v2, curl, openssl and python3. It refuses to run over an existing
# `.env`, so it will never overwrite the configuration of a deployment you already have, and it
# tears the stack down again on every exit path.
#
# Everything it needs, it generates: there is no secret to supply and nothing to configure.

set -euo pipefail

# The host port published by docker-compose.yml when CASSETTA_PORT is left at its default, which
# is how `.env.example` ships it. The port inside the container is fixed at 16001.
readonly HOST_PORT=16001
readonly BASE_URL="http://localhost:${HOST_PORT}"

readonly HEALTH_TIMEOUT_SECONDS=180
readonly HEALTH_POLL_SECONDS=2

# The dev placeholder `.env.example` ships. Substituting it is a step that must fail loudly if
# its subject is absent, rather than quietly producing an unconfigured `.env`.
readonly SETUP_TOKEN_PLACEHOLDER='change-me-to-a-long-random-string'

# Where the API key store lands on the host, given the shipped bind mounts.
readonly KEY_STORE='data.keys/.cassetta-keys.json'

# The unprivileged account the image creates, and the id the application process must end up with.
readonly EXPECTED_APP_UID=1001

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly REPO_ROOT
cd -- "$REPO_ROOT"

# --- output -----------------------------------------------------------------------------------

step() { printf '\n=== %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }

# First argument is the headline; any further arguments are printed as indented detail lines.
die() {
    printf '\nsmoke: FAILED — %s\n' "$1" >&2
    shift
    local line
    for line in "$@"; do
        printf '                %s\n' "$line" >&2
    done
    exit 1
}

# --- teardown ---------------------------------------------------------------------------------

env_file_created=false
stack_started=false

# Dump logs before tearing down: a scheduled run nobody is watching is worthless if its output
# does not say what broke. Then take the stack down whatever the outcome, and remove the `.env`
# this run created — and only that one.
cleanup() {
    local status=$?
    set +e

    if [ "$stack_started" = true ]; then
        if [ "$status" -ne 0 ]; then
            printf '\n=== container logs (exit status %s)\n' "$status" >&2
            docker compose logs --no-color >&2
        fi
        printf '\n=== tearing the stack down\n'
        docker compose down --remove-orphans >/dev/null 2>&1
    fi

    if [ "$env_file_created" = true ]; then
        rm -f -- "$REPO_ROOT/.env"
    fi
}
trap cleanup EXIT

# --- HTTP -------------------------------------------------------------------------------------

# One request, split into body and status code. The status is appended on its own final line so
# that a multi-line body survives intact. Sets HTTP_STATUS and HTTP_BODY.
http() {
    local method="$1" url="$2"
    shift 2
    local response
    response="$(curl -sS -X "$method" -w $'\n%{http_code}' "$@" "$url")" ||
        die "${method} ${url} — the request could not be made at all"
    HTTP_STATUS="${response##*$'\n'}"
    HTTP_BODY="${response%$'\n'*}"
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found on PATH: $1"
}

# --- pre-flight -------------------------------------------------------------------------------
#
# Everything here runs before the image build, so a foreseeable failure costs seconds rather than
# minutes of build time.

step "Pre-flight"

require_command docker
require_command curl
require_command openssl
require_command sed
# The read-back assertion decodes a JSON envelope with the recipe docs/REST_API.md publishes,
# and that recipe is a python3 one-liner. Required here so a host without it fails in seconds
# rather than after an image build, a stack start and a round trip.
require_command python3

docker compose version >/dev/null 2>&1 ||
    die "'docker compose' is unavailable" \
        "This script needs Compose v2, as the README quickstart does."

[ -f .env.example ] || die ".env.example not found in ${PWD}" \
    "Run this script from a checkout of the repository."
[ -f docker-compose.yml ] || die "docker-compose.yml not found in ${PWD}" \
    "Run this script from a checkout of the repository."

if [ -e .env ]; then
    die "refusing to overwrite an existing .env" \
        "This script writes its own .env with a throwaway setup token, and yours may belong" \
        "to a running deployment. Move it aside first:  mv .env .env.mine"
fi

if [ -e "$KEY_STORE" ]; then
    die "a previous run's key store survives at ${KEY_STORE}" \
        "POST /setup is one-time and the flag is persisted, so minting a key would answer 409." \
        "Remove that file and run again."
fi

info "prerequisites present, working tree clean of previous runs"

# The application runs as uid 1001 (see Dockerfile). On native Linux a bind mount passes ownership
# through literally, so the directories this script creates below belong to whoever ran it, and the
# container has to cope with that difference — which the entrypoint does, by correcting the two
# mount points before it drops privileges. On macOS the runtime remaps ownership and the question
# never arises, which is why a green run here proves less than a green run on Linux.
#
# Printed because it says which of those two cases this run is: an id other than 1001 means the
# correction is being exercised. Diagnostic only — nothing here gates on it, and the script
# deliberately does not chown anything or override the container's user.
invoking_uid="$(id -u)"
[ -n "$invoking_uid" ] || die "could not determine the invoking user id (id -u)"

info "invoking user id: ${invoking_uid} (the application runs as uid ${EXPECTED_APP_UID})"

# --- environment ------------------------------------------------------------------------------

step "Preparing .env from .env.example"

grep -q "^CASSETTA_SETUP_TOKEN=${SETUP_TOKEN_PLACEHOLDER}\$" .env.example ||
    die "the shipped placeholder setup token was not found in .env.example" \
        "Expected a line reading: CASSETTA_SETUP_TOKEN=${SETUP_TOKEN_PLACEHOLDER}"

setup_token="$(openssl rand -hex 32)"
[ -n "$setup_token" ] || die "openssl produced an empty setup token"

env_file_created=true
sed "s|^CASSETTA_SETUP_TOKEN=.*|CASSETTA_SETUP_TOKEN=${setup_token}|" .env.example >.env

grep -q "^CASSETTA_SETUP_TOKEN=${setup_token}\$" .env ||
    die "the generated setup token did not reach .env"
if grep -q "${SETUP_TOKEN_PLACEHOLDER}" .env; then
    die "the shipped placeholder token survived into .env"
fi

info "wrote .env with a freshly generated setup token"

# --- stack ------------------------------------------------------------------------------------

step "Creating the bind-mount sources"

# Docker will not create these, which the README already explains.
mkdir -p data data.keys
info "data/ and data.keys/ ready"

step "Building the image and starting the stack"

stack_started=true
docker compose up -d --build

step "Waiting for ${BASE_URL}/health (up to ${HEALTH_TIMEOUT_SECONDS}s)"

# A bounded deadline rather than a fixed sleep: a sleep is either slow or flaky, and a timeout
# that says so is far easier to read than the HTTP error that would otherwise follow it.
deadline=$((SECONDS + HEALTH_TIMEOUT_SECONDS))
until curl -fsS --max-time 5 "${BASE_URL}/health" >/dev/null 2>&1; do
    if [ "$SECONDS" -ge "$deadline" ]; then
        die "the container did not answer ${BASE_URL}/health within ${HEALTH_TIMEOUT_SECONDS}s"
    fi
    sleep "$HEALTH_POLL_SECONDS"
done
info "healthy after ${SECONDS}s"

# --- the application is unprivileged ------------------------------------------------------------

step "Checking the application process is unprivileged"

# Asked of the container, not of the host. The pre-flight line above reports the *invoking* user's
# id, which says nothing about the process inside; and the image no longer declares a default user,
# so asking an exec session for its own id would answer 0 and prove nothing either. Those are
# different questions and only this one matters.
#
# PID 1 is the application: the entrypoint execs it, so the owner of PID 1 is the identity the
# service actually runs as. Checked before the round trip on purpose — if the privilege drop were
# ever removed, the round trip would still pass, because root can write anywhere, and this is the
# only assertion here that would catch it.
process_uid="$(docker compose exec -T cassetta \
    python -c 'import os; print(os.stat("/proc/1").st_uid)')" ||
    die "could not ask the container which user its application process belongs to"

[ "$process_uid" = "$EXPECTED_APP_UID" ] ||
    die "the application process runs as uid ${process_uid}, expected ${EXPECTED_APP_UID}" \
        "The entrypoint is meant to drop privileges before starting the application." \
        "uid 0 here means that drop was removed, or never happened."

info "the application process runs as uid ${process_uid}"

# --- mint a key -------------------------------------------------------------------------------

step "Minting the first API key (POST /setup)"

http POST "${BASE_URL}/setup" \
    -H "Content-Type: application/json" \
    -H "X-Setup-Token: ${setup_token}" \
    -d '{"host":"smoke","project":"check"}'

[ "$HTTP_STATUS" = "201" ] ||
    die "POST /setup answered ${HTTP_STATUS}, expected 201" "Response body: ${HTTP_BODY}"

# Extracted without jq: this script doubles as a usage example and should not imply a dependency
# the README never mentions. An absent field fails on the next line rather than sliding through.
api_key="$(printf '%s' "$HTTP_BODY" | sed -n 's/.*"api_key"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
[ -n "$api_key" ] || die "POST /setup returned no api_key field" "Response body: ${HTTP_BODY}"

info "minted a key for smoke:check"

# --- store and read back ------------------------------------------------------------------------

step "Storing a file and reading it back"

# A path unique to this run, so a file left by an earlier run can never answer the read and
# report a false pass.
stored_path="smoke/$(openssl rand -hex 8).txt"
payload="cassetta smoke $(date -u +%Y-%m-%dT%H:%M:%SZ)"

http PUT "${BASE_URL}/files/${stored_path}" \
    -H "Authorization: Bearer ${api_key}" \
    -H "Content-Type: text/plain" \
    --data-binary "${payload}"

[ "$HTTP_STATUS" = "201" ] ||
    die "PUT /files/${stored_path} answered ${HTTP_STATUS}, expected 201" \
        "Response body: ${HTTP_BODY}"

http GET "${BASE_URL}/files/${stored_path}" \
    -H "Authorization: Bearer ${api_key}"

[ "$HTTP_STATUS" = "200" ] ||
    die "GET /files/${stored_path} answered ${HTTP_STATUS}, expected 200" \
        "Response body: ${HTTP_BODY}"

# A read answers with a JSON envelope, not with raw bytes: a stored file is a one-file bundle,
# and the same envelope serves both. The bytes live in files[0].content, tagged utf8 or base64.
# This is the recipe docs/REST_API.md and the README quickstart both publish — reproduced rather
# than reinvented, so that a run of this script is also a test of the documented recipe.
#
# Its own exit status is the shape check. An envelope missing files, content or encoding — a
# reference envelope, say — and a body that is not JSON at all all make it exit non-zero with
# empty output, so a malformed response is reported here as a decode fault rather than sliding
# through as an empty string that then mismatches. Those are different faults, and a reader of a
# scheduled run cannot tell them apart from a diff of empty against non-empty.
decoded="$(printf '%s' "$HTTP_BODY" |
    python3 -c 'import sys,json,base64;d=json.load(sys.stdin)["files"][0];c=d["content"];sys.stdout.buffer.write(base64.b64decode(c) if d["encoding"]=="base64" else c.encode())')" ||
    die "the response to GET /files/${stored_path} could not be decoded as an inline envelope" \
        'Expected {"mode":"inline",…,"files":[{"content":…,"encoding":"utf8"|"base64"}]}.' \
        "Response body: ${HTTP_BODY}"

# The assertion that carries the test. Checking status codes alone would pass against a server
# that stored nothing at all.
[ "$decoded" = "$payload" ] ||
    die "the file read back differs from the file written" \
        "wrote: ${payload}" \
        "read:  ${decoded}"

info "round-tripped ${#payload} bytes at /files/${stored_path}, decoded from the envelope"

step "Smoke passed"
info "the image builds, the stack starts, and it serves authenticated traffic against a"
info "real mounted volume — the quickstart in README.md does what it says."
