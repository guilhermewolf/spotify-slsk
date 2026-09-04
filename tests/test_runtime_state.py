"""Unit tests for the in-process activity tracker + log ring buffer."""
import logging

from spotify_slsk import runtime_state


def _reset_buffer():
    """Tests share module state — clear it explicitly for determinism."""
    runtime_state._log_buffer.clear()
    runtime_state._log_seq = 0


class TestActivity:
    def test_default_activity_has_phase(self):
        snap = runtime_state.get_activity()
        assert "phase" in snap
        assert "age_seconds" in snap

    def test_set_activity_round_trip(self):
        runtime_state.set_activity(
            "downloading",
            detail="Foo — Bar",
            playlist_id="pl1",
            playlist_name="My Playlist",
            table_name="my_playlist",
            track_id="t1",
            track_name="Foo",
            track_artist="Bar",
        )
        snap = runtime_state.get_activity()
        assert snap["phase"] == "downloading"
        assert snap["detail"] == "Foo — Bar"
        assert snap["playlist_id"] == "pl1"
        assert snap["track_id"] == "t1"
        assert snap["age_seconds"] >= 0

    def test_set_activity_clears_unset_fields(self):
        runtime_state.set_activity(
            "downloading", track_id="t1", playlist_id="pl1"
        )
        runtime_state.set_activity("idle", detail="sleeping")
        snap = runtime_state.get_activity()
        assert snap["phase"] == "idle"
        assert snap["track_id"] is None
        assert snap["playlist_id"] is None


class TestRingBufferHandler:
    def setup_method(self):
        _reset_buffer()

    def test_handler_captures_log_records(self):
        handler = runtime_state.RingBufferHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger = logging.getLogger("test_ring")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            logger.info("hello world")
            logger.warning("uh oh")
        finally:
            logger.removeHandler(handler)

        snap = runtime_state.get_logs(since=0)
        assert snap["last_seq"] == 2
        assert len(snap["entries"]) == 2
        assert snap["entries"][0]["level"] == "INFO"
        assert "hello world" in snap["entries"][0]["message"]
        assert snap["entries"][1]["level"] == "WARNING"

    def test_get_logs_since_returns_only_newer(self):
        handler = runtime_state.RingBufferHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("test_since")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            logger.info("a")
            logger.info("b")
            mark = runtime_state.get_logs()["last_seq"]
            logger.info("c")
        finally:
            logger.removeHandler(handler)

        delta = runtime_state.get_logs(since=mark)
        msgs = [e["message"] for e in delta["entries"]]
        assert msgs == ["c"]

    def test_search_history_round_trip(self):
        runtime_state._search_history.clear()
        runtime_state.record_search("t1", "foo bar", result_count=5)
        runtime_state.record_search("t1", "foo", result_count=20)
        runtime_state.record_search("t2", "other", result_count=0)

        h1 = runtime_state.get_search_history("t1")
        assert [e["query"] for e in h1] == ["foo bar", "foo"]
        assert h1[0]["result_count"] == 5

        assert runtime_state.get_search_history("t2")[0]["query"] == "other"
        assert runtime_state.get_search_history("missing") == []

    def test_search_history_no_op_on_falsy_id(self):
        runtime_state._search_history.clear()
        runtime_state.record_search(None, "ignored")
        runtime_state.record_search("", "ignored")
        assert runtime_state._search_history == {}

    def test_search_history_caps_per_track(self):
        runtime_state._search_history.clear()
        for i in range(runtime_state.SEARCH_HISTORY_PER_TRACK + 5):
            runtime_state.record_search("t1", f"q{i}", result_count=i)
        h = runtime_state.get_search_history("t1")
        assert len(h) == runtime_state.SEARCH_HISTORY_PER_TRACK
        assert h[-1]["query"] == f"q{runtime_state.SEARCH_HISTORY_PER_TRACK + 4}"

    def test_buffer_is_bounded(self):
        handler = runtime_state.RingBufferHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger = logging.getLogger("test_bounded")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            for i in range(runtime_state.LOG_BUFFER_MAX + 50):
                logger.info(f"line {i}")
        finally:
            logger.removeHandler(handler)

        assert len(runtime_state._log_buffer) == runtime_state.LOG_BUFFER_MAX
        # Sequence numbers keep climbing even though oldest entries dropped.
        assert runtime_state._log_seq >= runtime_state.LOG_BUFFER_MAX + 50


class TestLoggingWiring:
    """setup_logging() is never exercised elsewhere in the suite.

    That gap let a broken intra-package import survive the whole test run:
    `from runtime_state import RingBufferHandler` inside setup_logging raised
    ModuleNotFoundError at runtime once the code moved into a package, while
    every test still passed. This covers the wiring, not just the buffer.
    """

    def test_setup_logging_installs_the_ring_buffer_handler(self, monkeypatch):
        import logging

        from spotify_slsk import log_config

        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        try:
            monkeypatch.setenv("LOGLEVEL", "INFO")
            log_config.setup_logging()  # must not raise

            assert any(
                type(h).__name__ == "RingBufferHandler" for h in root.handlers
            ), "ring buffer handler was not installed"
            # stdout handler must survive too — Docker reads that stream.
            assert any(
                isinstance(h, logging.StreamHandler)
                and type(h).__name__ != "RingBufferHandler"
                for h in root.handlers
            ), "stdout handler was replaced instead of supplemented"

            before = runtime_state.get_logs(since=0, limit=1)["last_seq"]
            logging.info("wiring probe")
            after = runtime_state.get_logs(since=before, limit=10)
            assert any("wiring probe" in e["message"] for e in after["entries"])
        finally:
            root.handlers, root.level = saved_handlers, saved_level
