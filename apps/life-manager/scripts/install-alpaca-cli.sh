#!/usr/bin/env bash
set -euo pipefail

version="0.0.14"
case "$(uname -m)" in
  x86_64|amd64)
    architecture="amd64"
    checksum="6c82ef31f94dd61aae1c90e40fc41fdfaf8111bd50e9a2780b9d8d304eb2ba66"
    ;;
  aarch64|arm64)
    architecture="arm64"
    checksum="621270e2b935dbae587e6ae05fe04a10bc178b4c9c638961a3d0214568ff2617"
    ;;
  *)
    echo "unsupported Alpaca CLI architecture" >&2
    exit 1
    ;;
esac

destination="${1:-.bin/alpaca}"
archive="$(mktemp)"
stage="$(mktemp -d)"
url="https://github.com/alpacahq/cli/releases/download/v${version}/cli_${version}_linux_${architecture}.tar.gz"
trap 'rm -f "$archive"; rm -rf "$stage"' EXIT

curl --fail --location --silent --show-error "$url" --output "$archive"
actual_checksum="$(sha256sum "$archive" | awk '{print $1}')"
if [[ "$actual_checksum" != "$checksum" ]]; then
  echo "Alpaca CLI checksum mismatch" >&2
  exit 1
fi
tar -xzf "$archive" -C "$stage" alpaca
mkdir -p "$(dirname "$destination")"
install -m 0755 "$stage/alpaca" "$destination"
if [[ "${ALPACA_CLI_INSTALL_VERIFY_ONLY:-false}" != "true" ]]; then
  test "$("$destination" version)" = "$version"
fi
