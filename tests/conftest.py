import pytest


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    # Every test gets its own state dir, so nothing reads or writes the real
    # ~/.vibesignal (colors, Matter config, sessions) of a running install.
    # Tests that set VIBECODING_SIGNAL_DIR themselves simply override this.
    monkeypatch.setenv("VIBECODING_SIGNAL_DIR", str(tmp_path / "vibesignal-state"))
