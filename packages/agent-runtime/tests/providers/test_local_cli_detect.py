from forge_agent.providers.local_cli.detect import CliName, detect_clis


def _which(found: dict[str, str]):
    return lambda name: found.get(name)


def test_detects_both_in_preference_order():
    found = _which({"claude": "/usr/local/bin/claude", "codex": "/usr/local/bin/codex"})
    result = detect_clis(preference=[CliName.codex, CliName.claude], which=found)
    assert [d.name for d in result] == [CliName.codex, CliName.claude]
    assert result[0].path == "/usr/local/bin/codex"


def test_returns_empty_when_nothing_installed():
    assert detect_clis(which=_which({})) == []


def test_never_raises_when_which_explodes():
    def boom(_name: str) -> str | None:
        raise OSError("PATH is on fire")

    assert detect_clis(which=boom) == []
