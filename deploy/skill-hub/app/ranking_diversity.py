"""Keep nearby source variants addressable while exposing varied candidates."""

def diversify(rows, name_of, query):
    """Defer repeated source-suffixed families within each 11-result window."""
    result = []
    query = (query or "").lower()
    for start in range(0, len(rows), 11):
        seen, first, deferred = set(), [], []
        for row in rows[start:start + 11]:
            name = name_of(row)
            base = name.split("--", 1)[0].lower()
            # A query naming a full source-specific id keeps the original order.
            if "--" in name and name.lower() in query:
                return rows
            if base in seen:
                deferred.append(row)
            else:
                seen.add(base)
                first.append(row)
        result.extend(first + deferred)
    return result
