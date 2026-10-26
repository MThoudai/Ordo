#!/bin/sh
set -eu
cat >/usr/share/nginx/html/runtime-config.js <<EOF
window.__ORDO_CONFIG__ = {
  pythonApiUrl: "${PYTHON_API_URL:-/api}"
};
EOF
exec nginx -g 'daemon off;'
