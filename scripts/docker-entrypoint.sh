#!/bin/sh
#
# Container entrypoint: make the two mounted directories writable by the application's user, then
# become that user and hand over.
#
# Why this exists. On Linux a bind mount passes host ownership through unchanged, and the quickstart
# creates ./data and ./data.keys with `mkdir -p` — so they belong to whoever ran it, and are
# writable by that user alone. The application runs as someone else and cannot write into its own
# data directory; the first stored file fails. Correcting that needs a privilege the application
# must never hold, so the container starts as root for the few lines below and drops before the
# application exists.
#
# Read those lines as a privileged program, because until the last one that is what they are.

set -eu

# The two bind-mount points, exactly as the image declares them (see the VOLUME line in the
# Dockerfile). Literals, and deliberately not taken from the environment: this script holds root
# while it touches them, and a path read from its environment would be a way to have root re-own an
# arbitrary directory.
readonly STORAGE_DIR=/data
readonly KEYSTORE_DIR=/data.keys

# The unprivileged account the image creates. Named here and resolved below rather than repeating
# the numbers the Dockerfile already writes down, so the two cannot drift apart.
readonly APP_USER=cassetta

readonly PREFIX=cassetta-entrypoint

# First argument is the headline; any further arguments are printed as indented detail lines.
# Everything goes to standard error, so it is distinguishable from the application's own first log
# lines in `docker compose logs`.
die() {
    printf '%s: %s\n' "$PREFIX" "$1" >&2
    shift
    for detail in "$@"; do
        printf '%s:   %s\n' "$PREFIX" "$detail" >&2
    done
    exit 1
}

note() {
    printf '%s: %s\n' "$PREFIX" "$1" >&2
}

[ "$#" -gt 0 ] || die "no command to run" \
    "The image supplies one as its default command; an empty one means it was overridden with" \
    "nothing. Nothing to start, so nothing is started."

# --- identities -----------------------------------------------------------------------------
#
# Read once, at the top, so that every branch below keys on the same reading.

entry_uid="$(id -u)"

app_uid="$(id -u "$APP_USER")" || die "the image has no ${APP_USER} account"
app_gid="$(id -g "$APP_USER")" || die "the image has no ${APP_USER} group"

# There is no path through this script that leaves the application running as root. If the account
# it is meant to become is itself root, the image is broken — a reason to stop, not a shortcut.
[ "$app_uid" -ne 0 ] || die "${APP_USER} resolves to uid 0" \
    "Refusing to start the application as root."

# The identity the application will actually end up with: the image's account while we still hold
# the privilege to change identity, and whoever the container was started as when we do not.
if [ "$entry_uid" -eq 0 ]; then
    run_uid="$app_uid"
else
    run_uid="$entry_uid"
fi

# Checked here rather than discovered at the handover. setpriv comes from util-linux in the base
# image; if it ever stopped being there, the writability check below would fail for a reason that
# has nothing to do with writability and would report that wrong reason confidently.
if [ "$entry_uid" -eq 0 ]; then
    command -v setpriv >/dev/null 2>&1 || die "setpriv is not available in this image" \
        "It is how this entrypoint gives up root, and there is no path here that starts the" \
        "application without giving up root first."
fi

# Run one command as that identity.
as_run_identity() {
    if [ "$entry_uid" -eq 0 ]; then
        setpriv --reuid="$app_uid" --regid="$app_gid" --clear-groups --no-new-privs -- "$@"
    else
        "$@"
    fi
}

# --- correct the mount points, where that is both possible and needed -------------------------

if [ "$entry_uid" -eq 0 ]; then
    # One level, on exactly these two directories. A recursive walk would descend into whatever the
    # host put inside them and, on meeting a symbolic link, change the ownership of something
    # outside the volume entirely — a privileged write to a path an unprivileged party chose. It is
    # also unnecessary: everything below these two is created by the application, as the
    # application, and a tree left by an earlier deployment was written the same way.
    #
    # Failure is reported and survived, on purpose. This step is not always needed — the volumes may
    # not be bind mounts, they may already be owned correctly, the filesystem may not carry
    # ownership at all — so stopping here would mean stopping at something that may not have
    # mattered, having named the wrong cause. The check below is the one that decides whether the
    # application can work.
    chown "${app_uid}:${app_gid}" "$STORAGE_DIR" "$KEYSTORE_DIR" ||
        note "could not change the ownership of ${STORAGE_DIR} and ${KEYSTORE_DIR}; continuing"
else
    # Rootless container tooling, or a policy that forbids root outright. The correction is
    # impossible here and also unnecessary: this process is already the one that will run the
    # application.
    note "started as uid ${entry_uid} rather than as root; leaving ownership alone"
fi

# --- the check that decides -------------------------------------------------------------------

# Asked as the identity the application will run as. Asked as root it would be meaningless: root can
# write into every directory on the system and would answer yes to all of them.
for mount_point in "$STORAGE_DIR" "$KEYSTORE_DIR"; do
    as_run_identity test -w "$mount_point" ||
        die "${mount_point} is not writable by uid ${run_uid}" \
            "That is the user the application runs as, and this is a directory it must write to." \
            "On Linux a bind mount passes the host's ownership through unchanged, so the directory" \
            "on the host has to be writable by that uid — and this container was not able to make" \
            "it so. Either start the container with the privilege to correct it, or give the host" \
            "directory to uid ${run_uid}."
done

# --- hand over --------------------------------------------------------------------------------

# exec, so the application replaces this script rather than running underneath it: it becomes the
# container's first process, and `docker compose down` stops it with a signal instead of waiting out
# a timeout.
#
# The two lines below are the only exits from this script that are not failures, and the first one
# drops. There is no variable, argument or branch above by which a container that started as root
# reaches the application still holding it. The arguments are forwarded unchanged, so a command
# supplied on the command line replaces the default one exactly as it did before — and has
# privileges dropped for it in the same way.
#
# --no-new-privs sets the kernel's no_new_privs bit, so neither the application nor anything it
# spawns can regain privilege by executing a setuid or setgid binary. A process that has just given
# up root deliberately has no reason to leave itself a route back. It is requested at every point
# this script changes identity, not only here.
if [ "$entry_uid" -eq 0 ]; then
    exec setpriv --reuid="$app_uid" --regid="$app_gid" --clear-groups --no-new-privs -- "$@"
fi

exec "$@"
