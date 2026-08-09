#!/usr/bin/env bash

SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(CDPATH='' cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
COMPOSE_FILE="${REPO_ROOT}/infra/local/compose.yml"
# shellcheck disable=SC2034
DEFAULT_MANIFEST="${REPO_ROOT}/infra/local/local-pilot-manifest.json"
RUNTIME_DIR="${REPO_ROOT}/.runtime/local-pilot"
SECRET_DIR_RECORD="${RUNTIME_DIR}/secret-dir"
CONFIG_DIR_RECORD="${RUNTIME_DIR}/config-dir"
# shellcheck disable=SC2034
EMERGENCY_STOP_FILE="${RUNTIME_DIR}/EMERGENCY_STOP"
PROJECT_NAME="esan-gbos-local-pilot"

compose() {
  docker compose \
    --project-name "${PROJECT_NAME}" \
    -f "${COMPOSE_FILE}" \
    "$@"
}

read_secret_dir() {
  if [[ -f "${SECRET_DIR_RECORD}" ]]; then
    IFS= read -r GBOS_SECRET_DIR < "${SECRET_DIR_RECORD}"
    export GBOS_SECRET_DIR
  fi
}

read_config_dir() {
  if [[ -f "${CONFIG_DIR_RECORD}" ]]; then
    IFS= read -r GBOS_CONFIG_DIR < "${CONFIG_DIR_RECORD}"
    export GBOS_CONFIG_DIR
  fi
}

cleanup_secret_dir() {
  local secret_dir=
  local secret_tmp_root="${TMPDIR:-/tmp}"
  secret_tmp_root="${secret_tmp_root%/}"
  if [[ -f "${SECRET_DIR_RECORD}" ]]; then
    IFS= read -r secret_dir < "${SECRET_DIR_RECORD}"
  fi
  case "${secret_dir}" in
    "${secret_tmp_root}"/gbos-local-pilot-secrets.*|/tmp/gbos-local-pilot-secrets.*|/private/tmp/gbos-local-pilot-secrets.*)
      if [[ -d "${secret_dir}" ]]; then
        rm -f -- "${secret_dir}"/*
        rmdir -- "${secret_dir}"
      fi
      ;;
    "")
      ;;
    *)
      echo "Refusing to remove unexpected secret directory: ${secret_dir}" >&2
      return 78
      ;;
  esac
  rm -f -- "${SECRET_DIR_RECORD}"
}

cleanup_config_dir() {
  local config_dir=
  local config_tmp_root="${TMPDIR:-/tmp}"
  config_tmp_root="${config_tmp_root%/}"
  if [[ -f "${CONFIG_DIR_RECORD}" ]]; then
    IFS= read -r config_dir < "${CONFIG_DIR_RECORD}"
  fi
  case "${config_dir}" in
    "${config_tmp_root}"/gbos-local-pilot-config.*|/tmp/gbos-local-pilot-config.*|/private/tmp/gbos-local-pilot-config.*)
      if [[ -d "${config_dir}" ]]; then
        rm -f -- "${config_dir}"/*.json
        rmdir -- "${config_dir}"
      fi
      ;;
    "")
      ;;
    *)
      echo "Refusing to remove unexpected config directory: ${config_dir}" >&2
      return 78
      ;;
  esac
  rm -f -- "${CONFIG_DIR_RECORD}"
}
