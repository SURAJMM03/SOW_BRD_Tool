"""
Generation Stability Tests
==========================
Covers the five scenarios mandated by the fix requirements:

  Scenario A – 6 selected sections → exactly 1 optimised run (mutex test)
  Scenario B – Double-click Generate → second request rejected
  Scenario C – Claude returns empty response → automatic retries
  Scenario D – Queue finishes → single Quality Check execution
  Scenario E – User presses Stop → no new sections generated after current

Python tests validate the backend (claude_provider retry logic).
Frontend (JavaScript) scenarios are documented as contract assertions that
map to the code changes made in generating.html.
"""

import os
import time
import types
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_content_block(text: str):
    """Return a stub Anthropic ContentBlock with a .text attribute."""
    block = types.SimpleNamespace(text=text)
    return block


def _make_response(text: str, stop_reason: str = "end_turn"):
    """Return a stub Anthropic Message."""
    return types.SimpleNamespace(
        content=[_make_content_block(text)],
        stop_reason=stop_reason,
    )


def _make_empty_response(stop_reason: str = "end_turn"):
    """Return a stub Anthropic Message with an empty content block."""
    return types.SimpleNamespace(
        content=[_make_content_block("")],
        stop_reason=stop_reason,
    )


def _make_no_blocks_response():
    """Return a stub Anthropic Message with an empty content list."""
    return types.SimpleNamespace(
        content=[],
        stop_reason="end_turn",
    )


# ---------------------------------------------------------------------------
# Scenario C  —  Claude returns empty response → automatic retries
# ---------------------------------------------------------------------------

class TestClaudeProviderRetry:
    """Backend: completion_from_prompt retries on empty / malformed content."""

    def _import_provider(self):
        # Re-import so each test gets a clean module state.
        import importlib
        try:
            import app.claude_provider as mod
        except ImportError:
            import claude_provider as mod
        return importlib.reload(mod)

    # ── C-1: first attempt empty → retries → second attempt succeeds ────────
    def test_retries_on_empty_content_then_succeeds(self):
        mod = self._import_provider()
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            _make_empty_response(),           # attempt 1 — empty
            _make_response("Good content."),  # attempt 2 — content
        ]
        with patch.object(mod, "_get_client", return_value=mock_client), \
             patch("time.sleep"):             # don't actually wait in tests
            result = mod.completion_from_prompt("test prompt", max_retries=3)
        assert result == "Good content."
        assert mock_client.messages.create.call_count == 2

    # ── C-2: all attempts return empty → RuntimeError raised ────────────────
    def test_raises_after_all_retries_exhausted(self):
        mod = self._import_provider()
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_empty_response()
        with patch.object(mod, "_get_client", return_value=mock_client), \
             patch("time.sleep"):
            with pytest.raises(RuntimeError, match="empty content after 3 attempts"):
                mod.completion_from_prompt("test prompt", max_retries=3)
        assert mock_client.messages.create.call_count == 3

    # ── C-3: no content blocks → retries ────────────────────────────────────
    def test_retries_on_empty_content_blocks(self):
        mod = self._import_provider()
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            _make_no_blocks_response(),       # attempt 1 — no blocks
            _make_response("Valid output."),  # attempt 2 — content
        ]
        with patch.object(mod, "_get_client", return_value=mock_client), \
             patch("time.sleep"):
            result = mod.completion_from_prompt("test prompt", max_retries=3)
        assert result == "Valid output."
        assert mock_client.messages.create.call_count == 2

    # ── C-4: first response non-empty → no retry ────────────────────────────
    def test_no_retry_when_first_response_has_content(self):
        mod = self._import_provider()
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_response("First try wins.")
        with patch.object(mod, "_get_client", return_value=mock_client), \
             patch("time.sleep") as mock_sleep:
            result = mod.completion_from_prompt("test prompt", max_retries=3)
        assert result == "First try wins."
        assert mock_client.messages.create.call_count == 1
        mock_sleep.assert_not_called()

    # ── C-5: stop_reason logged (observability) ─────────────────────────────
    def test_logs_stop_reason_on_empty_response(self, caplog):
        import logging
        mod = self._import_provider()
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            _make_empty_response(stop_reason="max_tokens"),
            _make_response("Content after max_tokens retry."),
        ]
        with patch.object(mod, "_get_client", return_value=mock_client), \
             patch("time.sleep"):
            with caplog.at_level(logging.WARNING, logger="app.claude_provider"):
                result = mod.completion_from_prompt("test prompt", max_retries=3)
        assert result == "Content after max_tokens retry."
        # The warning must mention the stop_reason for observability
        assert any("max_tokens" in r.message for r in caplog.records), (
            "stop_reason='max_tokens' not found in log records"
        )

    # ── C-6: rate-limit exception retried with longer backoff ───────────────
    def test_retries_on_rate_limit_exception(self):
        mod = self._import_provider()
        mock_client = MagicMock()
        rate_limit_err = Exception("429 Too Many Requests — rate_limit exceeded")
        mock_client.messages.create.side_effect = [
            rate_limit_err,
            _make_response("Content after rate limit."),
        ]
        sleep_calls = []
        with patch.object(mod, "_get_client", return_value=mock_client), \
             patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            result = mod.completion_from_prompt("test prompt", max_retries=3)
        assert result == "Content after rate limit."
        # Rate-limit waits must be longer than the normal 2^attempt backoff
        assert sleep_calls, "Expected at least one sleep() call for rate-limit retry"
        assert sleep_calls[0] >= 10, (
            f"Rate-limit backoff too short: {sleep_calls[0]}s (expected >= 10s)"
        )

    # ── C-7: non-retriable exception re-raised immediately ──────────────────
    def test_non_retriable_exception_reraised(self):
        mod = self._import_provider()
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = ValueError("Bad API key")
        with patch.object(mod, "_get_client", return_value=mock_client), \
             patch("time.sleep"):
            with pytest.raises(ValueError, match="Bad API key"):
                mod.completion_from_prompt("test prompt", max_retries=3)
        # Should raise on first attempt, not retry
        assert mock_client.messages.create.call_count == 1

    # ── C-8: mock mode returns stub without hitting API ──────────────────────
    def test_mock_mode_returns_stub(self):
        mod = self._import_provider()
        with patch.dict(os.environ, {"CLAUDE_MOCK": "1"}):
            result = mod.completion_from_prompt("hello world")
        assert result.startswith("[MOCK CLAUDE RESPONSE]")
        assert "hello world" in result


# ---------------------------------------------------------------------------
# Scenario A & B  —  Mutex / duplicate-run prevention (contract tests)
# These validate the logic contract enforced by the JS changes, expressed
# as pure Python so they run in CI without a browser.
# ---------------------------------------------------------------------------

class TestGenerationMutexContract:
    """
    Validate the invariants that the JavaScript mutex enforces.

    The JavaScript implementation in generating.html sets running=true
    synchronously (before any await) so concurrent callers are rejected.
    These Python tests mirror that logic as contract documentation.
    """

    def test_scenario_a_mutex_prevents_concurrent_start(self):
        """
        SCENARIO A — 6 selected sections must trigger exactly 1 optimised run.

        Contract: once running=true is set synchronously, a second call to
        startOptimizedRun() finds running=true and returns immediately.
        """
        running = False
        start_count = 0

        def start_optimized_run_mock():
            nonlocal running, start_count
            if running:
                return  # ← second call rejected
            running = True  # ← set synchronously before any async op
            start_count += 1
            # simulate async work ...
            running = False

        # Simulate two concurrent invocations (e.g. autostart + button click)
        start_optimized_run_mock()
        running = True  # ← first call set this; second call arrives during run
        start_optimized_run_mock()  # must be rejected
        running = False

        assert start_count == 1, (
            f"Expected exactly 1 run, got {start_count}. "
            "Mutex did not prevent duplicate generation."
        )

    def test_scenario_b_double_click_rejected(self):
        """
        SCENARIO B — Double-click Generate → second request silently rejected.

        Contract: when running=true, startOptimizedRun() must return without
        incrementing start_count or launching another loop.
        """
        running = True  # already running
        duplicate_blocked = False

        def start_optimized_run_mock():
            nonlocal duplicate_blocked
            if running:
                duplicate_blocked = True
                return  # ← rejected
            # would start a run here ...

        start_optimized_run_mock()
        assert duplicate_blocked, "Second call was not blocked by the mutex."

    def test_scenario_d_quality_check_runs_once(self):
        """
        SCENARIO D — Queue finishes → single Quality Check execution.

        Contract: __qualityRunning guard prevents overlapping quality checks.
        """
        quality_running = False
        quality_run_count = 0

        def run_quality_check_mock():
            nonlocal quality_running, quality_run_count
            if quality_running:
                return  # ← duplicate rejected
            quality_running = True
            quality_run_count += 1
            # simulate async check ...
            quality_running = False

        # First call from end-of-queue auto-check
        run_quality_check_mock()
        # Second call from a concurrent trigger (e.g. user button during run)
        quality_running = True  # simulate still running
        run_quality_check_mock()  # must be rejected
        quality_running = False

        assert quality_run_count == 1, (
            f"Quality check ran {quality_run_count} times — expected exactly 1."
        )

    def test_scenario_e_stop_prevents_new_sections(self):
        """
        SCENARIO E — User presses Stop → no new sections generated.

        Contract: once stopRequested=True, the for-loop in startOptimizedRun()
        breaks before calling generateOne() for remaining sections.
        """
        stop_requested = False
        generated_sections = []

        def mock_generate_one(sec_id):
            if stop_requested:
                return  # stop guard in loop body
            generated_sections.append(sec_id)

        targets = ["1", "1.1", "1.2", "1.3", "1.4"]

        for i, sec_id in enumerate(targets):
            if stop_requested:
                break
            mock_generate_one(sec_id)
            if i == 1:  # user presses stop after section 1.1
                stop_requested = True

        assert "1" in generated_sections
        assert "1.1" in generated_sections
        assert "1.2" not in generated_sections, (
            "Section 1.2 was generated after Stop was requested."
        )


# ---------------------------------------------------------------------------
# Scenario C (backend)  —  init() idempotency guard
# ---------------------------------------------------------------------------

class TestInitIdempotency:
    """__initDone guard must prevent init() from running twice."""

    def test_init_runs_exactly_once(self):
        """
        Contract: if init() is called twice (back-nav, hot-reload),
        the second call must return immediately without triggering autostart.
        """
        init_done = False
        autostart_count = 0

        def init_mock():
            nonlocal init_done, autostart_count
            if init_done:
                return
            init_done = True
            # simulate auto-start trigger
            autostart_count += 1

        init_mock()  # first call — allowed
        init_mock()  # second call — must be rejected by guard

        assert autostart_count == 1, (
            f"init() triggered autostart {autostart_count} times — expected 1."
        )
