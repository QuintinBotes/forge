"""#187 criteria 1 and 7: the secret gate reads a diff's added lines.

Every credential below is fake and split, so no source line holds one whole: the
repository's own gitleaks scan needs no allowance for this file.
"""

from __future__ import annotations

import logging

import pydantic
import pytest

from scf_contracts import Policy
from scf_policy.diff_gate import GateFinding, added_lines
from scf_policy.secret_gate import scan_secrets

FAKE = {
    "aws-access-token": "AKIA" + "QZ7X" * 4,
    "github-token": "ghp_" + "Zx9" * 12,
    "private-key": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "slack-token": "xoxb-" + "1234567890-abcdef",
    "stripe-secret-key": "sk_" + "live_" + "Ab3" * 9,
    "google-api-key": "AIza" + "Sy" + "B7" * 16 + "x",
    "npm-access-token": "npm_" + "Qw8" * 12,
}


def _diff(value: str) -> str:
    return (
        "diff --git a/app/settings.py b/app/settings.py\n"
        "index 1111111..2222222 100644\n"
        "--- a/app/settings.py\n"
        "+++ b/app/settings.py\n"
        "@@ -1 +1 @@\n"
        '-REGION = "us-east-1"\n'
        '+REGION = "eu-west-1"\n'
        "@@ -9,0 +10,2 @@ def settings():\n"
        "+    # the deploy key\n"
        f'+    ACCESS_KEY = "{value}"\n'
    )


def test_a_diff_that_adds_a_credential_fails_with_file_line_and_rule() -> None:
    result = scan_secrets(added_lines(_diff(FAKE["aws-access-token"])))

    assert result.status == "failed" and not result.passed
    assert result.findings == [
        GateFinding(file="app/settings.py", line=11, rule="aws-access-token")
    ]


@pytest.mark.parametrize("rule", sorted(FAKE))
def test_each_credential_shape_is_found_under_its_rule(rule: str) -> None:
    result = scan_secrets(added_lines(_diff(FAKE[rule])))

    assert [f.rule for f in result.findings] == [rule]


@pytest.mark.parametrize("rule", sorted(FAKE))
def test_the_value_is_never_in_the_finding_the_report_or_a_log_line(
    rule: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    result = scan_secrets(added_lines(_diff(FAKE[rule])))

    assert not result.passed
    assert FAKE[rule] not in result.model_dump_json()
    assert FAKE[rule] not in result.report()
    assert FAKE[rule] not in caplog.text


def test_only_added_lines_are_scanned() -> None:
    removed = (
        "diff --git a/app/old.py b/app/old.py\n"
        "deleted file mode 100644\n"
        "--- a/app/old.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        f'-KEY = "{FAKE["aws-access-token"]}"\n'
    )

    assert added_lines(removed) == {}
    assert scan_secrets(added_lines(removed)).passed


def test_ordinary_code_is_not_a_credential() -> None:
    lines = [
        "ACCESS_KEY = os.environ['AWS_ACCESS_KEY_ID']",
        "-----BEGIN PUBLIC KEY-----",
        "token = 'ghp_short'",
        "slack_url = 'https://hooks.slack.com/services/T000'",
    ]

    assert scan_secrets({"app/ok.py": list(enumerate(lines, start=1))}).passed


def test_a_line_that_starts_with_plus_signs_is_content_not_a_header() -> None:
    diff = (
        "diff --git a/notes.md b/notes.md\n"
        "--- a/notes.md\n"
        "+++ b/notes.md\n"
        "@@ -0,0 +1,2 @@\n"
        "+++ heading\n"
        f"+{FAKE['github-token']}\n"
    )

    assert added_lines(diff) == {"notes.md": [(1, "++ heading"), (2, FAKE["github-token"])]}


def test_a_policy_can_allow_a_match_in_one_named_file_and_the_result_says_so() -> None:
    added = added_lines(_diff(FAKE["aws-access-token"]))
    rules = Policy.model_validate(
        {"repo_id": "r", "diff_gates": {"secret_allowed_files": ["app/settings.py"]}}
    ).diff_gates

    result = scan_secrets(added, allowed_files=rules.secret_allowed_files)

    assert result.passed and result.findings == []
    assert result.allowed == [GateFinding(file="app/settings.py", line=11, rule="aws-access-token")]
    assert "allowed by policy: app/settings.py:11 [aws-access-token]" in result.report()


@pytest.mark.parametrize("named", ["app", "app/settings.p", "settings.py", "APP/settings.py"])
def test_an_allowance_covers_only_the_file_it_names(named: str) -> None:
    result = scan_secrets(added_lines(_diff(FAKE["aws-access-token"])), allowed_files=[named])

    assert not result.passed and result.allowed == []


@pytest.mark.parametrize(
    "path", ["app/*", "app/", "app/**/settings.py", "/app/settings.py", "../settings.py", ""]
)
def test_a_wildcard_or_directory_allowance_is_refused_when_the_policy_loads(path: str) -> None:
    with pytest.raises(pydantic.ValidationError, match="secret_allowed_files"):
        Policy.model_validate({"repo_id": "r", "diff_gates": {"secret_allowed_files": [path]}})
