"""Tests for logging_setup.py and the swallowed-exception logging."""

import logging

import pytest

# ── configure() tests ────────────────────────────────────────────────────────


class TestConfigure:
    def test_configure_twice_produces_one_set_of_handlers(self, monkeypatch):
        """Calling configure() twice must not double every line."""
        from agent_server.logging_setup import configure

        # Reset the module-level guard so we can test from a clean slate.
        monkeypatch.setattr("agent_server.logging_setup._configured", False)
        root = logging.getLogger()
        # Detach any handlers added by previous tests.
        root.handlers.clear()

        configure()
        first = len(root.handlers)
        assert first > 0, "expected at least one handler after configure()"

        configure()
        assert len(root.handlers) == first, (
            f"expected {first} handlers, got {len(root.handlers)} after second call"
        )

    @pytest.mark.parametrize("env_val,expected", [
        ("DEBUG", logging.DEBUG),
        ("debug", logging.DEBUG),
        ("WARNING", logging.WARNING),
        ("warning", logging.WARNING),
    ])
    def test_codeagent_log_level_is_honoured(self, monkeypatch, env_val, expected):
        """CODEAGENT_LOG_LEVEL is respected, and lowercase works."""
        from agent_server.logging_setup import configure

        monkeypatch.setattr("agent_server.logging_setup._configured", False)
        monkeypatch.setenv("CODEAGENT_LOG_LEVEL", env_val)
        root = logging.getLogger()
        root.handlers.clear()

        configure()
        assert root.level == expected, (
            f"expected level {expected}, got {root.level} for CODEAGENT_LOG_LEVEL={env_val!r}"
        )

    def test_default_level_is_info(self, monkeypatch):
        """Without CODEAGENT_LOG_LEVEL, we default to INFO."""
        from agent_server.logging_setup import configure

        monkeypatch.setattr("agent_server.logging_setup._configured", False)
        monkeypatch.delenv("CODEAGENT_LOG_LEVEL", raising=False)
        root = logging.getLogger()
        root.handlers.clear()

        configure()
        assert root.level == logging.INFO


# ── file handler ─────────────────────────────────────────────────────────────


class TestFileHandler:
    def test_file_handler_writes_to_data_dir(self, tmp_path, monkeypatch):
        """The rotating file handler writes to DATA_DIR / 'codeagent.log'."""
        from agent_server.logging_setup import configure

        monkeypatch.setattr("agent_server.logging_setup._configured", False)
        monkeypatch.setattr("agent_server.logging_setup.DATA_DIR", tmp_path)
        root = logging.getLogger()
        root.handlers.clear()

        configure()
        root.warning("file handler test message")

        log_file = tmp_path / "codeagent.log"
        assert log_file.exists(), f"expected {log_file} to exist after log call"
        content = log_file.read_text()
        assert "file handler test message" in content


# ── swallowed exception is recorded ──────────────────────────────────────────


class TestSwallowedException:
    def test_describe_image_file_logs_on_failure(self, caplog):
        """describe_image_file swallows exceptions and logs at debug."""
        from agent_server.images import describe_image_file

        # Both the child and root loggers must allow DEBUG to pass through.
        logging.getLogger().setLevel(logging.DEBUG)
        logging.getLogger("agent_server.images").setLevel(logging.DEBUG)
        caplog.set_level(logging.DEBUG)

        result = describe_image_file("/nonexistent/not-an-image.xyz")
        assert result == "image"

        records = [r for r in caplog.records if r.name == "agent_server.images"]
        assert len(records) >= 1, "expected at least one log record from vision module"
        assert records[0].levelno == logging.DEBUG
        assert "reading image dimensions" in records[0].message


# ── third-party loggers are quietened ────────────────────────────────────────


class TestThirdPartyQuieten:
    @pytest.mark.parametrize("name", [
        "httpx", "httpcore", "urllib3", "asyncio",
        "playwright", "watchfiles", "multipart", "PIL",
    ])
    def test_third_party_logger_is_at_warning(self, monkeypatch, name):
        """After configure(), noisy third-party loggers are at WARNING."""
        from agent_server.logging_setup import configure

        monkeypatch.setattr("agent_server.logging_setup._configured", False)
        root = logging.getLogger()
        root.handlers.clear()

        configure()

        lg = logging.getLogger(name)
        assert lg.level == logging.WARNING, (
            f"expected {name} logger level WARNING, got {lg.level}"
        )
