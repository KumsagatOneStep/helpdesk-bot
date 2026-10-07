_FIELDS = {"name": "full_name", "location": "location"}


def parse_client_args(args: str | None) -> tuple[int, str, str] | None:
    if not args:
        return None
    parts = args.split(maxsplit=2)
    if len(parts) != 3:
        return None
    num, field, value = parts[0].lstrip("#"), parts[1].lower(), parts[2].strip()
    if not num.isdigit() or field not in _FIELDS or not 1 <= len(value) <= 200:
        return None
    return int(num), _FIELDS[field], value
