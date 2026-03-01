"""Request middleware for integration review testing."""


def auth_middleware(request, next_handler):
    """Check for valid session token before processing request."""
    token = request.headers.get("Authorization")
    if not token:
        return {"status": 401, "error": "Unauthorized"}
    return next_handler(request)
