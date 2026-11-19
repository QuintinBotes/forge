"""Where a governed workflow run's context records what was verified.

The worker writes these keys and the attestation service reads them (#175), so
both import the names from here rather than repeating them.
"""

from __future__ import annotations

__all__ = [
    "CHECKS_KEY",
    "HELD_OUT_CHECK",
    "HELD_OUT_KEY",
    "INDEPENDENT_REVIEW_CHECK",
    "PULL_REQUEST_KEY",
    "REVIEWS_KEY",
]

#: Each check attempt's evidence: command ids, outcomes and the head commit (#172).
CHECKS_KEY = "checks"
#: The pull request the run opened, ``{"number", "url"}``; its issue is told of it (#278).
PULL_REQUEST_KEY = "pull_request"
#: Each independent review's verdict, findings and model ids (#173).
REVIEWS_KEY = "reviews"
#: The check id the independent review is reported under (#173).
INDEPENDENT_REVIEW_CHECK = "independent_review"
#: The check id the held-out scenarios are reported under, as one check (#286).
HELD_OUT_CHECK = "held_out_scenarios"
#: On the run, why no held-out scenario ran (#258, #286); on a check attempt, the
#: held-out counts ``{"verdict", "defined", "passed", "failed"}``. Never ids or titles.
HELD_OUT_KEY = "held_out"
