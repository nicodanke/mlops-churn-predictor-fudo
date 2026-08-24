#!/bin/sh
# Escribe la URL de la API como un script que el dashboard lee al cargar.
# Permite reusar la misma imagen entre entornos sin recompilar el front.
set -e
cat > /usr/share/nginx/html/config.js <<EOF
window.CHURN_API_URL = "${CHURN_API_URL:-http://localhost:8000}";
EOF
echo "Dashboard apuntando a la API en ${CHURN_API_URL:-http://localhost:8000}"
