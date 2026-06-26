"""Pin the cycle-3 trim of the _MINIMAL_DIFF_RULE motivational wrapper.

The "REFLEX, NOT RESEARCH" paragraph was trimmed to its behavioral core
(take the lazier rung, don't deliberate across turns), dropping the motivational
rationale. The load-bearing safety carve-outs MUST remain intact.
"""

from scripts.dispatch import _MINIMAL_DIFF_RULE


def test_reflex_wrapper_trimmed():
    # The motivational rationale is gone...
    assert "research project" not in _MINIMAL_DIFF_RULE
    assert "deliberation burns" not in _MINIMAL_DIFF_RULE
    # ...but the behavioral instruction survives.
    assert "don't deliberate across turns" in _MINIMAL_DIFF_RULE
    assert "take the higher (lazier)" in _MINIMAL_DIFF_RULE


def test_load_bearing_carveouts_preserved():
    # ai-safety MUST-KEEP items.
    assert "WHEN NOT TO BE LAZY" in _MINIMAL_DIFF_RULE
    assert "COVER EVERY REQUIREMENT" in _MINIMAL_DIFF_RULE
    assert "input validation at trust boundaries" in _MINIMAL_DIFF_RULE
