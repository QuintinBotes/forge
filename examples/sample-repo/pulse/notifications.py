# ruff: noqa: F821 - illustrative sample snippet (undefined helpers) for RAG indexing
"""Outbound notifications for the Pulse service."""


def notify_slack(channel, message):
    # post a slack notification message to a channel via an incoming webhook url
    return slack_client.post(channel=channel, text=message)
