"""Decorators for payment operations."""

import functools

from payments.config import RETRY_LIMIT


def cached(func):
    """Cache decorator for expensive operations."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper


def retry(max_attempts=RETRY_LIMIT):
    """Retry decorator for transient failures."""

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception:
                    if attempt == max_attempts - 1:
                        raise
            return None

        return wrapper

    return decorator
