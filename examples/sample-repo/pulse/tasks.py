# ruff: noqa: F821 - illustrative sample snippet (undefined helpers) for RAG indexing
"""Task lifecycle operations for the Pulse service."""


def create_task(title, assignee):
    # persist a brand new task record into the database with a pending status
    return repository.insert(Task(title=title, assignee=assignee))


def list_tasks(status):
    # query and return tasks filtered by their current workflow status value
    return repository.query(status=status)
