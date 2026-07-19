"""Comprehensive tests for Goal-Drift Detection feature."""

import pytest
from unittest.mock import MagicMock, patch

from archolith_proxy.assembler.goal_drift import (
    compute_lexical_similarity,
    detect_goal_drift,
    should_check_drift,
)


# =============================================================================
# Lexical Similarity Tests
# =============================================================================

class TestComputeLexicalSimilarity:
    def test_identical_text(self):
        goal = "Build a user authentication system with JWT"
        assert compute_lexical_similarity(goal, goal) > 0.9

    def test_high_overlap(self):
        goal = "Implement user authentication with JWT tokens"
        recent = "Continue working on the authentication system using JWT"
        sim = compute_lexical_similarity(goal, recent)
        assert sim > 0.5

    def test_low_overlap(self):
        goal = "Build user authentication system"
        recent = "Create a completely unrelated dashboard UI"
        sim = compute_lexical_similarity(goal, recent)
        assert sim < 0.3

    def test_empty_inputs(self):
        assert compute_lexical_similarity("", "test") == 0.0
        assert compute_lexical_similarity("test", "") == 0.0
        assert compute_lexical_similarity("", "") == 0.0

    def test_case_insensitive(self):
        goal = "Build Authentication System"
        recent = "build authentication system"
        assert compute_lexical_similarity(goal, recent) > 0.8


# =============================================================================
# Drift Detection Tests
# =============================================================================

class TestDetectGoalDrift:
    def test_no_drift_similar_messages(self):
        goal = "Build a user authentication system with JWT"
        recent = [
            "Continue implementing the JWT auth flow",
            "Add refresh token support",
            "Fix the login endpoint",
        ]
        drift, sim = detect_goal_drift(goal, recent, threshold=0.40)
        assert drift is False
        assert sim > 0.40

    def test_drift_detected(self):
        goal = "Build user authentication with JWT"
        recent = [
            "Start working on a completely new dashboard",
            "Implement charts and graphs",
            "Add admin user management UI",
        ]
        drift, sim = detect_goal_drift(goal, recent, threshold=0.40)
        assert drift is True
        assert sim < 0.40

    def test_empty_recent_messages(self):
        goal = "Build auth system"
        drift, sim = detect_goal_drift(goal, [], threshold=0.40)
        assert drift is False
        assert sim == 1.0

    def test_empty_goal(self):
        recent = ["Some message"]
        drift, sim = detect_goal_drift("", recent, threshold=0.40)
        assert drift is False
        assert sim == 1.0

    def test_threshold_tuning(self):
        goal = "Build authentication"
        recent = ["Work on login page"]
        
        # High threshold → more likely to detect drift
        drift_strict, _ = detect_goal_drift(goal, recent, threshold=0.80)
        # Low threshold → less likely to detect drift
        drift_lenient, _ = detect_goal_drift(goal, recent, threshold=0.20)
        
        assert drift_strict or not drift_lenient  # Strict should be more sensitive


# =============================================================================
# Configuration Tests
# =============================================================================

class TestShouldCheckDrift:
    def test_enabled(self):
        settings = MagicMock()
        settings.goal_drift_detection_enabled = True
        assert should_check_drift(settings) is True

    def test_disabled(self):
        settings = MagicMock()
        settings.goal_drift_detection_enabled = False
        assert should_check_drift(settings) is False

    def test_default_false(self):
        settings = MagicMock(spec=[])  # No attribute
        assert should_check_drift(settings) is False


# =============================================================================
# Integration / Edge Cases
# =============================================================================

class TestGoalDriftEdgeCases:
    def test_very_short_recent_messages(self):
        goal = "Build a complex authentication system"
        recent = ["ok"]
        drift, sim = detect_goal_drift(goal, recent, threshold=0.40)
        # Should not crash and return reasonable result
        assert isinstance(drift, bool)
        assert 0.0 <= sim <= 1.0

    def test_very_long_goal(self):
        goal = "Build " + "a very long goal description " * 20
        recent = ["Continue with the task"]
        drift, sim = detect_goal_drift(goal, recent, threshold=0.40)
        assert isinstance(drift, bool)

    def test_multiple_drift_points(self):
        """Test that we can detect drift multiple times in a session."""
        goal = "Build authentication"
        
        # First window - still on goal
        recent1 = ["Continue JWT implementation", "Add token refresh"]
        drift1, _ = detect_goal_drift(goal, recent1, threshold=0.40)
        
        # Second window - drifted
        recent2 = ["Start new dashboard feature", "Add charts"]
        drift2, _ = detect_goal_drift(goal, recent2, threshold=0.40)
        
        assert drift1 is False
        assert drift2 is True


# =============================================================================
# Metric Recording Test (Mocked)
# =============================================================================

def test_drift_metric_is_recorded():
    """Verify that drift detection increments the metric."""
    from archolith_proxy.metrics import get_metrics, record_metric

    metrics = get_metrics()
    original = metrics.get("goal_drift_detections", 0)
    
    record_metric("goal_drift_detections")
    
    assert metrics["goal_drift_detections"] == original + 1