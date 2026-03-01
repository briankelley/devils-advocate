# Security Requirements Reference

## Authentication
- All secrets must be stored in environment variables or a secrets manager
- Password hashing must use bcrypt or argon2 (not SHA-256)
- Session tokens must be cryptographically random (not MD5)
- Sessions must expire after 30 minutes of inactivity

## Data Access
- All database queries must use parameterized statements
- No string interpolation in SQL queries
- Input validation on all user-supplied data

## API Security
- HTTPS required for all endpoints
- CORS whitelist must be explicit (no wildcard)
- Rate limiting on authentication endpoints
