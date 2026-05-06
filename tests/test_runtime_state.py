"""Unit tests for the in-process activity tracker + log ring buffer."""
import logging

import runtime_state


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
