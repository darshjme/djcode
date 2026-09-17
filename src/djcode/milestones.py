"""Validation for user-reported roadmap progress."""


def validate_milestone_status(status: str) -> str:
    if not isinstance(status, str):
        raise ValueError("Status must be a string.")

    valid_statuses = ["planned", "in_progress", "blocked", "done"]

    if status not in valid_statuses:
        raise ValueError(
            "Invalid status. Status must be one of 'planned', 'in_progress', 'blocked', 'done'."
        )

    return status
