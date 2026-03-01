# Authentication Middleware Plan

## Overview
Add JWT-based authentication middleware to the API gateway. All endpoints
except `/health` and `/auth/login` will require a valid Bearer token.

## Steps
1. Install `pyjwt` dependency
2. Create `middleware/auth.py` with token validation
3. Add `@require_auth` decorator to protected routes
4. Store signing key in environment variable `JWT_SECRET`
5. Add token refresh endpoint at `/auth/refresh`
6. Update OpenAPI spec with security schemes

## Considerations
- Token expiry set to 15 minutes, refresh tokens to 7 days
- Rate limiting on `/auth/login` to prevent brute force
- No session storage — stateless JWT only
- HMAC-SHA256 signing (symmetric key)
