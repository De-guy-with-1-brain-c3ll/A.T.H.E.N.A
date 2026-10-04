#!/bin/sh
# Put the dashboard on HTTPS, generating the certificate it serves.
#
# Browsers only expose the microphone in a "secure context", and a plain http://
# address on the local network is not one. That is what blocks Start under
# "Microphone and speaker" on a computer or phone -- and music fails with it,
# because the speaker attaches over the same socket. Serving the dashboard over
# TLS is the fix; localhost would need none, which is why this only bites on a
# LAN address.
#
# The certificate is self-signed, so every device that opens the dashboard must
# trust it once. On Windows, run "Trust Dashboard Certificate.cmd" as
# administrator, then reopen the browser.
#
# Usage: dashboard_tls.sh [on|off] [--force] [extra-name ...]
#   on           enable HTTPS (the default)
#   off          revert to plain HTTP
#   --force      rebuild the certificate even if one already exists
#   extra-name   cover an extra address, e.g. a reserved IP or a hostname
set -eu

DIR=/etc/athena/tls
CERT="$DIR/dashboard.crt"
KEY="$DIR/dashboard.key"
ENV_FILE=/etc/athena/athena.env
DASHBOARD_USER=athena

say() { printf '%s\n' "$*"; }

set_env() {
  name=$1
  value=$2
  if grep -q "^${name}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${name}=.*|${name}=${value}|" "$ENV_FILE"
  else
    if [ -s "$ENV_FILE" ] && [ -n "$(tail -c 1 "$ENV_FILE")" ]; then
      printf '\n' >> "$ENV_FILE"
    fi
    printf '%s=%s\n' "$name" "$value" >> "$ENV_FILE"
  fi
}

# The dashboard runs unprivileged, so anything it has to read must be readable
# by its group -- including every directory on the way in. Getting this wrong
# looks exactly like a broken certificate, so it is checked rather than assumed.
apply_permissions() {
  if [ -d "$DIR" ]; then
    chown root:"$DASHBOARD_USER" "$DIR"
    chmod 0750 "$DIR"
  fi
  if [ -f "$CERT" ]; then
    chown root:"$DASHBOARD_USER" "$CERT"
    chmod 0644 "$CERT"
  fi
  if [ -f "$KEY" ]; then
    chown root:"$DASHBOARD_USER" "$KEY"
    chmod 0640 "$KEY"
  fi
}

check_readable() {
  if su -s /bin/sh "$DASHBOARD_USER" -c "test -r '$KEY'" 2>/dev/null; then
    return 0
  fi
  say "The dashboard user cannot read $KEY, so it would refuse to start."
  say "Directory and file ownership, for reference:"
  ls -ld "$DIR" "$KEY" || true
  return 1
}

# A crash-looping unit is "active" for a moment between restarts, so activity
# alone proves nothing. Wait for the health endpoint to answer instead.
restart_dashboard() {
  scheme=$1
  systemctl restart athena-web.service
  attempt=0
  while [ "$attempt" -lt 12 ]; do
    attempt=$((attempt + 1))
    sleep 2
    if ! systemctl is-active --quiet athena-web.service; then
      continue
    fi
    if ! command -v curl >/dev/null 2>&1; then
      say "Restarted. curl is not installed, so the health check was skipped."
      return 0
    fi
    code=$(curl -sk -o /dev/null -w '%{http_code}' "${scheme}://127.0.0.1:8780/health" || true)
    if [ "$code" = "200" ]; then
      say "Health check over ${scheme}: 200"
      return 0
    fi
  done
  say "The dashboard did not answer over ${scheme}. Last lines of its log:"
  journalctl -u athena-web.service -n 20 --no-pager || true
  return 1
}

if [ "$(id -u)" != 0 ]; then
  say "Run this as root: sudo $0 $*"
  exit 1
fi

OFF=0
FORCE=0
EXTRA=""
for argument in "$@"; do
  case "$argument" in
    on) ;;
    off|--off) OFF=1 ;;
    --force) FORCE=1 ;;
    *) EXTRA="$EXTRA $argument" ;;
  esac
done

if [ "$OFF" = 1 ]; then
  say "Reverting the dashboard to plain HTTP."
  grep -v '^ATHENA_WEB_TLS_' "$ENV_FILE" > "${ENV_FILE}.tls-new" || true
  cat "${ENV_FILE}.tls-new" > "$ENV_FILE"
  rm -f "${ENV_FILE}.tls-new"
  restart_dashboard http || exit 1
  say "Done. The microphone is blocked again until HTTPS is switched back on."
  exit 0
fi

# Every name the browser might use to reach this box has to be in the
# certificate: the browser refuses a certificate that does not match the address
# typed in, and a refused certificate is not a secure context either.
san=""
add_name() {
  case ",$san," in
    *",$1,"*) return 0 ;;
  esac
  if [ -n "$san" ]; then
    san="$san,$1"
  else
    san="$1"
  fi
}

add_name "DNS:localhost"
add_name "IP:127.0.0.1"
host=$(hostname)
if [ -n "$host" ]; then
  add_name "DNS:$host"
fi
for address in $(hostname -I 2>/dev/null); do
  add_name "IP:$address"
done
for name in $EXTRA; do
  case "$name" in
    *[!0-9.]*) add_name "DNS:$name" ;;
    *) add_name "IP:$name" ;;
  esac
done

if [ -f "$CERT" ] && [ -f "$KEY" ] && [ "$FORCE" != 1 ]; then
  say "Keeping the existing certificate."
  # openssl prints IP entries as "IP Address:1.2.3.4" while the request above says
  # "IP:1.2.3.4", so normalise before comparing or every IP looks uncovered.
  covered=$(openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null \
            | tr -d '\n' | sed 's/IP Address:/IP:/g' || true)
  missing=""
  for entry in $(printf '%s' "$san" | tr ',' ' '); do
    case "$covered" in
      *"$entry"*) ;;
      *) missing="$missing $entry" ;;
    esac
  done
  if [ -n "$missing" ]; then
    say "It does not cover:$missing"
    say "If one of those is the address you open, re-run with --force and then"
    say "trust the new certificate again. A DHCP reservation for this box avoids"
    say "this entirely."
  fi
else
  say "Generating a certificate for: $san"
  mkdir -p "$DIR"
  chmod 0750 "$DIR"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$KEY" -out "$CERT" \
    -subj "/CN=${host:-athena}" \
    -addext "subjectAltName=$san" \
    -addext "keyUsage=digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth" >/dev/null 2>&1
fi

apply_permissions
check_readable || exit 1

set_env ATHENA_WEB_TLS_CERT "$CERT"
set_env ATHENA_WEB_TLS_KEY "$KEY"
say "Restarting the dashboard."
restart_dashboard https || exit 1

fingerprint=$(openssl x509 -in "$CERT" -noout -fingerprint -sha256 | sed 's/^[^=]*=//')
set -- $(hostname -I)
say ""
say "Certificate : $CERT"
say "SHA-256     : $fingerprint"
say "Open        : https://${1:-<this-box>}:8780"
say ""
say "Next: trust the certificate on every device that opens the dashboard, or the"
say "browser will refuse it -- and a refused certificate blocks the microphone too."
say "Windows: run 'Trust Dashboard Certificate.cmd' as administrator, then reopen"
say "the browser. On a phone, install the certificate as a trusted root."
