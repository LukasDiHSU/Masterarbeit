# shellcheck shell=bash
# Load agent_project/.env into the launch shell.
#
# Without this the launch scripts never see .env: only the Python processes read
# it, and every value the launcher exports itself (AGENT_WORLD, AGENT_COUNT, …)
# then silently overrides the file, because a real environment variable beats
# the .env file (agentpackage/__init__.py uses os.environ.setdefault).
#
# Same precedence as Python: something already set in the shell always wins.

_load_dotenv() {
  local file="${1:-}"
  [ -f "$file" ] || return 0

  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in
      ''|'#'*) continue ;;
      *=*) ;;
      *) continue ;;
    esac
    key="${line%%=*}"
    value="${line#*=}"
    key="${key//[[:space:]]/}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    case "$value" in
      \"*\"|\'*\') value="${value:1:${#value}-2}" ;;
    esac
    if [ -z "${!key+x}" ]; then
      export "$key=$value"
    fi
  done < "$file"
}
