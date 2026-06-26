def parse_work_rest_counts(value):
    text = (value or "").replace("、", ",")
    try:
        counts = [int(part.strip()) for part in text.split(",") if part.strip()]
    except ValueError:
        return []
    if not counts or len(counts) % 2 or any(count < 1 for count in counts):
        return []
    return counts


def work_rest_pattern_from_counts(counts):
    pattern = []
    for index, count in enumerate(counts):
        pattern.extend([index % 2 == 0] * count)
    return tuple(pattern)


def work_rest_pattern_from_text(value):
    return work_rest_pattern_from_counts(parse_work_rest_counts(value))


def previous_work_statuses(previous_days):
    result = []
    for item in previous_days:
        if item.status == "work":
            result.append(True)
        elif item.status in {"public_holiday", "paid_leave"}:
            result.append(False)
    return result


def next_pattern_index_from_previous(pattern, previous_statuses):
    if not pattern or not previous_statuses:
        return 0
    tail = previous_statuses[-len(pattern) :]
    best_start = 0
    best_score = -1
    for start in range(len(pattern)):
        score = sum(
            1
            for offset, status in enumerate(tail)
            if pattern[(start + offset) % len(pattern)] == status
        )
        if score > best_score:
            best_score = score
            best_start = start
    return (best_start + len(tail)) % len(pattern)


def previous_consecutive_work_count(previous_days):
    count = 0
    for item in reversed(previous_days):
        if item.status == "work":
            count += 1
            continue
        if item.status in {"public_holiday", "paid_leave", "blank"}:
            break
    return count
