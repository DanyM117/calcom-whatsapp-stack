#!/usr/bin/env bash
set -euo pipefail

DOMAIN_NAME="${1:-poccitas.sv-solution.com}"
APP_BASE_URL="https://${DOMAIN_NAME}"

# 1. Clonar fuente de Cal.com solo si no existe (para compilar calcom-api)
if [ ! -d "calcom-src" ]; then
    git clone --depth 1 https://github.com/calcom/cal.com.git calcom-src
fi

# 2. Generar .env con secretos criptográficos si es un despliegue nuevo
if [ ! -f ".env" ]; then
    SECRET_NEXTAUTH=$(openssl rand -base64 32)
    SECRET_CALENDSO=$(openssl rand -base64 24)
    SECRET_JWT=$(openssl rand -hex 32)
    DB_PASS=$(openssl rand -hex 16)
    DB_CONN="postgresql://calcom_user:${DB_PASS}@database:5432/calendso"

    cat <<ENV_EOF > .env
NODE_ENV="production"
NEXTAUTH_SECRET="${SECRET_NEXTAUTH}"
CALENDSO_ENCRYPTION_KEY="${SECRET_CALENDSO}"
JWT_SECRET="${SECRET_JWT}"
POSTGRES_USER="calcom_user"
POSTGRES_PASSWORD="${DB_PASS}"
POSTGRES_DB="calendso"
DATABASE_HOST="database:5432"
DATABASE_URL="${DB_CONN}"
DATABASE_DIRECT_URL="${DB_CONN}"
DATABASE_READ_URL="${DB_CONN}"
DATABASE_WRITE_URL="${DB_CONN}"
NEXT_PUBLIC_WEBAPP_URL="${APP_BASE_URL}"
NEXTAUTH_URL="${APP_BASE_URL}/api/auth"
NEXT_PUBLIC_API_V2_URL="${APP_BASE_URL}/api/v2"
WEB_APP_URL="${APP_BASE_URL}"
AWS_REGION="us-east-1"
API_PORT=80
API_URL="http://calcom-api:80"
REDIS_URL="redis://redis:6379"
CALCOM_TELEMETRY_DISABLED=1
STRIPE_API_KEY="sk_test_placeholder_local_only"
STRIPE_WEBHOOK_SECRET="whsec_placeholder_local_only"
IS_TEAM_BILLING_ENABLED="false"
GOOGLE_API_CREDENTIALS=""
WHATSAPP_VERIFY_TOKEN="bedrock_haiku_token_2026"
WHATSAPP_ACCESS_TOKEN=""
WHATSAPP_APP_SECRET=""
ENV_EOF
    chmod 600 .env
    echo "[OK] .env generado. Completa GOOGLE_API_CREDENTIALS, WHATSAPP_ACCESS_TOKEN y WHATSAPP_APP_SECRET antes de activar Meta."
fi

# 3. Levantar base de datos y esperar disponibilidad
export DOCKER_BUILDKIT=1
docker compose up -d database redis
until docker compose exec -T database pg_isready -U calcom_user -d calendso >/dev/null 2>&1; do
    sleep 3
done

# 4. Compilar y levantar servicios
docker compose up -d --build
until [ "$(docker inspect -f '{{.State.Running}}' calcom 2>/dev/null)" = "true" ]; do
    sleep 3
done

# 5. Migraciones de Prisma + tabla multi-tenant del agente
docker compose exec -T calcom yarn workspace @calcom/prisma db-deploy
docker compose exec -T database psql -U calcom_user -d calendso < db/01_agent_tenants.sql

docker compose restart calcom calcom-api whatsapp-agent nginx
echo "[OK] Stack operativo."
