#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID=""
LOCATION="global"
MODEL_ID="google/gemma-4-26b-a4b-it-maas"
STREAM_MODE="synthetic"
INSTALL_DIR="${HOME}/vertex-proxy"
LINUX_USER="${USER:-$(id -un)}"
SKIP_HERMES_INSTALL="auto"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_BIN=""

usage() {
  cat <<'EOF'
Usage:
  bash scripts/bootstrap-vm.sh --project-id YOUR_PROJECT_ID [options]

Options:
  --project-id   Google Cloud project ID. Required.
  --location     Vertex AI location. Default: global
  --model-id     Vertex model ID. Default: google/gemma-4-26b-a4b-it-maas
  --stream-mode  Proxy stream mode. Default: synthetic
  --install-dir  Proxy install directory. Default: $HOME/vertex-proxy
  --linux-user   Linux username for the systemd service. Default: current user
  --skip-hermes  Skip Hermes installation entirely
  --force-hermes-install  Run Hermes installer even if Hermes already exists
  -h, --help     Show this help
EOF
}

log() {
  printf '\n==> %s\n' "$1"
}

fail() {
  printf '\nERROR: %s\n' "$1" >&2
  exit 1
}

ensure_local_bin_on_path() {
  local path_line='export PATH="$HOME/.local/bin:$PATH"'
  if [[ -f "${HOME}/.bashrc" ]] && ! grep -Fq "${path_line}" "${HOME}/.bashrc"; then
    printf '\n%s\n' "${path_line}" >> "${HOME}/.bashrc"
  fi

  export PATH="${HOME}/.local/bin:${PATH}"
}

detect_hermes_bin() {
  if command -v hermes >/dev/null 2>&1; then
    HERMES_BIN="$(command -v hermes)"
    return 0
  fi

  for candidate in \
    "${HOME}/.local/bin/hermes" \
    "${HOME}/.hermes/hermes-agent/venv/bin/hermes" \
    "/usr/local/bin/hermes"
  do
    if [[ -x "${candidate}" ]]; then
      HERMES_BIN="${candidate}"
      return 0
    fi
  done

  HERMES_BIN=""
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project-id)
      PROJECT_ID="${2:-}"
      shift 2
      ;;
    --location)
      LOCATION="${2:-}"
      shift 2
      ;;
    --model-id)
      MODEL_ID="${2:-}"
      shift 2
      ;;
    --stream-mode)
      STREAM_MODE="${2:-}"
      shift 2
      ;;
    --install-dir)
      INSTALL_DIR="${2:-}"
      shift 2
      ;;
    --linux-user)
      LINUX_USER="${2:-}"
      shift 2
      ;;
    --skip-hermes)
      SKIP_HERMES_INSTALL="yes"
      shift
      ;;
    --force-hermes-install)
      SKIP_HERMES_INSTALL="no"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown argument: $1"
      ;;
  esac
done

[[ -n "${PROJECT_ID}" ]] || fail "--project-id is required"

log "Installing OS packages"
sudo apt update
sudo apt install -y git python3-venv python3-pip curl
ensure_local_bin_on_path

if [[ "${SKIP_HERMES_INSTALL}" == "yes" ]]; then
  if ! detect_hermes_bin; then
    fail "--skip-hermes was used, but Hermes is not installed on this VM"
  fi
  log "Skipping Hermes installation by request"
elif command -v hermes >/dev/null 2>&1 && [[ "${SKIP_HERMES_INSTALL}" == "auto" ]]; then
  detect_hermes_bin || true
  log "Hermes already exists on this VM, skipping reinstall"
else
  log "Installing Hermes Agent"
  curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --skip-setup

  if [[ -f "${HOME}/.bashrc" ]]; then
    # shellcheck disable=SC1090
    source "${HOME}/.bashrc" || true
  fi

  ensure_local_bin_on_path
  detect_hermes_bin || fail "Hermes installation finished, but the hermes binary was not found"
fi

log "Preparing proxy directory"
mkdir -p "${INSTALL_DIR}"
cp "${REPO_ROOT}/vertex_openai_proxy.py" "${INSTALL_DIR}/vertex_openai_proxy.py"
cp "${REPO_ROOT}/requirements-proxy.txt" "${INSTALL_DIR}/requirements-proxy.txt"

log "Creating Python virtual environment"
python3 -m venv "${INSTALL_DIR}/.venv"
"${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements-proxy.txt"

log "Writing proxy environment file"
cat > "${INSTALL_DIR}/proxy.env" <<EOF
PROJECT_ID=${PROJECT_ID}
LOCATION=${LOCATION}
MODEL_ID=${MODEL_ID}
VERTEX_STREAM_MODE=${STREAM_MODE}
EOF

log "Installing systemd service"
sudo tee /etc/systemd/system/vertex-openai-proxy.service >/dev/null <<EOF
[Unit]
Description=Vertex OpenAI Proxy for Hermes
After=network-online.target
Wants=network-online.target

[Service]
User=${LINUX_USER}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/proxy.env
ExecStart=${INSTALL_DIR}/.venv/bin/uvicorn vertex_openai_proxy:app --host 127.0.0.1 --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

log "Starting proxy service"
sudo systemctl daemon-reload
sudo systemctl enable vertex-openai-proxy.service
sudo systemctl restart vertex-openai-proxy.service

log "Verifying proxy health"
curl --fail --silent http://127.0.0.1:8080/healthz >/dev/null
curl --fail --silent http://127.0.0.1:8080/v1/models >/dev/null

cat <<EOF

Bootstrap complete.

Next step:
  hermes model

Hermes binary:
  ${HERMES_BIN:-not found}

Use:
  URL:   http://127.0.0.1:8080/v1
  Model: ${MODEL_ID}
  API key: leave blank

Useful checks:
  systemctl status vertex-openai-proxy.service --no-pager
  curl http://127.0.0.1:8080/healthz
  curl http://127.0.0.1:8080/v1/models
  source ~/.bashrc

EOF
