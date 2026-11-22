CREATE TABLE IF NOT EXISTS agent_tenants (
    id SERIAL PRIMARY KEY,
    phone_number_id VARCHAR(50) UNIQUE NOT NULL,
    calcom_api_key VARCHAR(255) NOT NULL DEFAULT 'public_v2',
    event_type_id INT NOT NULL,
    timezone VARCHAR(50) DEFAULT 'America/Monterrey',
    business_name VARCHAR(100),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
