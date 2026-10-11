# Sourced only by the official runner through BASH_ENV. Its PATH reset would
# otherwise bypass a private bin directory, and it overwrites $DOCKER itself.
which() {
    if [ "$#" -eq 1 ] && [ "$1" = docker ]; then
        printf '%s\n' "${COWORK_DOCKER_SHIM:?missing run-scoped Docker shim}"
    else
        command which "$@"
    fi
}
# Child shells and the shim need no startup injection. The runner already owns
# this function; its task subshells inherit the resolved absolute $DOCKER path.
unset BASH_ENV
