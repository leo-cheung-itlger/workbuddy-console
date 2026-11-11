"""Recognize confirmed daily check-in results across callers."""


def checkin_succeeded(result):
    return isinstance(result, dict) and (
        result.get('checked') is True or
        result.get('status') in ('checked', 'already_done', 'already_checked', 'already_signed')
    )
