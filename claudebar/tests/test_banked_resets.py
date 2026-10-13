"""Banked resets: parsing both providers' responses, the count rule, and which
reset (if any) may be spent.

The fixtures are the response shapes recorded from the real accounts on
2026-10-12 (Claude: one grant expiring Oct 22; Codex: three credits expiring
Oct 22, Oct 29 and Nov 6). Nothing here touches the network.
"""

from datetime import datetime, timedelta, timezone

import pytest

import banked_resets as br

UTC = timezone.utc
NOW = datetime(2026, 10, 12, 18, 30, tzinfo=UTC)
OCT22 = datetime(2026, 10, 22, 16, 0, tzinfo=UTC)


def grant(**kw):
    base = {
        "id": "opus55-launch-promax-20260921",
        "label": "Claude Opus 5.5 launch: one usage-limit reset for Pro and Max",
        "resets_total": 1, "resets_left": 1,
        "starts_at": "2026-09-21T00:00:00+00:00",
        "ends_at": "2026-10-22T16:00:00+00:00",
        "clears": ["five_hour", "seven_day", "seven_day_overage_included"],
        "paused": False, "usable_now": True, "use_requires_limit": False,
    }
    base.update(kw)
    return base


def claude_block(*grants, **kw):
    first = next((g["id"] for g in grants if isinstance(g, dict) and "id" in g),
                 "opus55-launch-promax-20260921")
    block = {
        "eligible": True, "ineligible_reason": None, "at_limit": False, "exhausted": [],
        "grants": list(grants) or [grant()],
        "next_grant_id": first,
        "weekly_resets_at": "2026-10-16T09:00:00+00:00", "cooldown_until": None,
    }
    block.update(kw)
    return block


def credit(cid, expires, status="available", **kw):
    base = {"id": cid, "reset_type": "codex_rate_limits", "status": status,
            "title": "Full reset (Monthly)", "granted_at": "2026-10-01T00:00:00Z",
            "expires_at": expires}
    base.update(kw)
    return base


def codex_details(*credits, **kw):
    body = {"available_count": len([c for c in credits
                                    if isinstance(c, dict) and c.get("status") == "available"]),
            "credits": list(credits), "total_earned_count": 9,
            "history_enabled": True, "immediate_reset_purchase_eligible": False}
    body.update(kw)
    return body


USAGE_BLOCK = {"available_count": 3, "applicable_available_count": 0}
THREE_CREDITS = (credit("c-nov6", "2026-11-06T09:00:00Z"),
                 credit("c-oct22", "2026-10-22T09:00:00Z"),
                 credit("c-oct29", "2026-10-29T09:00:00Z"))


class TestExpiry:
    def test_iso_with_z_and_with_offset_agree(self):
        assert br.parse_expiry("2026-10-22T16:00:00Z") == OCT22
        assert br.parse_expiry("2026-10-22T16:00:00+00:00") == OCT22

    def test_an_offset_is_converted_to_utc(self):
        assert br.parse_expiry("2026-10-22T19:00:00+03:00") == OCT22

    def test_a_naive_stamp_is_read_as_utc(self):
        assert br.parse_expiry("2026-10-22T16:00:00") == OCT22

    def test_epoch_seconds_and_milliseconds_agree(self):
        seconds = int(OCT22.timestamp())
        assert br.parse_expiry(seconds) == OCT22
        assert br.parse_expiry(seconds * 1000) == OCT22

    @pytest.mark.parametrize("bad", [None, "", "soon", True, False, [], {}, float("nan")])
    def test_anything_else_is_none(self, bad):
        assert br.parse_expiry(bad) is None


class TestClaudeParser:
    def test_the_live_block(self):
        b = br.parse_claude_banked(claude_block(), NOW)
        assert b.provider == "claude" and b.state == "ok"
        assert b.available == 1
        assert [(c.id, c.expires_at) for c in b.credits] == [
            ("opus55-launch-promax-20260921", OCT22)]
        assert b.credits[0].clears == ("five_hour", "seven_day", "seven_day_overage_included")
        assert b.next_credit_id == "opus55-launch-promax-20260921"

    def test_count_is_the_sum_of_resets_left_not_the_number_of_grants(self):
        b = br.parse_claude_banked(claude_block(
            grant(id="a", resets_total=3, resets_left=2),
            grant(id="b", resets_total=4, resets_left=3)), NOW)
        assert len(b.credits) == 2
        assert b.available == 5

    def test_a_malformed_grant_is_dropped_without_losing_its_neighbours(self):
        no_id = grant()
        del no_id["id"]
        no_left = grant(id="no-left")
        del no_left["resets_left"]
        b = br.parse_claude_banked(claude_block(
            no_id,
            grant(id="good-1"),
            grant(id="zero-total", resets_total=0, resets_left=0),
            no_left,
            grant(id="bad-date", ends_at="garbage"),
            "not a dict",
            grant(id="bool-total", resets_total=True),
            grant(id="good-2", resets_total=2, resets_left=2),
        ), NOW)
        assert [c.id for c in b.credits] == ["good-1", "good-2"]
        assert b.available == 3

    def test_resets_left_above_the_total_is_clamped(self):
        b = br.parse_claude_banked(claude_block(grant(resets_total=2, resets_left=5)), NOW)
        assert b.credits[0].count == 2
        assert b.available == 2

    def test_a_negative_resets_left_is_an_exhausted_grant(self):
        b = br.parse_claude_banked(claude_block(grant(resets_left=-4)), NOW)
        assert b.credits == [] and b.available == 0

    def test_a_grant_with_nothing_left_is_omitted(self):
        b = br.parse_claude_banked(claude_block(
            grant(id="spent", resets_total=1, resets_left=0), grant(id="live")), NOW)
        assert [c.id for c in b.credits] == ["live"]
        assert b.available == 1

    def test_an_expired_grant_is_not_counted(self):
        b = br.parse_claude_banked(claude_block(
            grant(id="old", ends_at="2026-10-01T00:00:00+00:00"),
            grant(id="new")), NOW)
        assert b.available == 1

    def test_a_paused_grant_is_not_counted(self):
        b = br.parse_claude_banked(claude_block(
            grant(id="held", paused=True), grant(id="free", resets_total=2, resets_left=2)), NOW)
        assert b.available == 2

    def test_cooldown_is_parsed(self):
        b = br.parse_claude_banked(
            claude_block(cooldown_until="2026-10-12T20:00:00+00:00"), NOW)
        assert b.cooldown_until == datetime(2026, 10, 12, 20, 0, tzinfo=UTC)

    def test_not_eligible_is_remembered_and_has_no_resets(self):
        b = br.parse_claude_banked(
            {"eligible": False, "ineligible_reason": "surface", "grants": [grant()]}, NOW)
        assert b.state == "ineligible" and b.reason == "surface"
        assert b.available == 0 and b.credits == []

    @pytest.mark.parametrize("block", [None, "x", 5, [], [claude_block()]])
    def test_a_missing_or_non_object_block_is_none(self, block):
        assert br.parse_claude_banked(block, NOW) is None

    def test_epoch_expiries_are_accepted(self):
        seconds = int(OCT22.timestamp())
        b = br.parse_claude_banked(claude_block(
            grant(id="s", ends_at=seconds), grant(id="ms", ends_at=seconds * 1000)), NOW)
        assert [(c.id, c.expires_at) for c in b.credits] == [("s", OCT22), ("ms", OCT22)]

    def test_credits_are_ordered_earliest_expiry_first_with_their_own_ids(self):
        b = br.parse_claude_banked(claude_block(
            grant(id="late", ends_at="2026-11-30T00:00:00+00:00"),
            grant(id="soon", ends_at="2026-10-15T00:00:00+00:00"),
            grant(id="mid", ends_at="2026-11-01T00:00:00+00:00")), NOW)
        assert [(c.id, c.expires_at.day) for c in b.credits] == [
            ("soon", 15), ("mid", 1), ("late", 30)]

    def test_a_non_list_grants_field_is_an_empty_set(self):
        b = br.parse_claude_banked(claude_block(grants="oops"), NOW)
        assert b.state == "ok" and b.available == 0


class TestCodexParser:
    def test_the_live_response_is_sorted_earliest_first(self):
        b = br.parse_codex_banked(USAGE_BLOCK, codex_details(*THREE_CREDITS), NOW)
        assert b.provider == "codex" and b.state == "ok"
        assert b.available == 3 and b.applicable == 0
        assert [(c.id, c.expires_at.month, c.expires_at.day) for c in b.credits] == [
            ("c-oct22", 10, 22), ("c-oct29", 10, 29), ("c-nov6", 11, 6)]
        assert b.credits[0].label == "Full reset (Monthly)"

    def test_the_usage_count_is_authoritative_not_the_list_length(self):
        b = br.parse_codex_banked({"available_count": 2, "applicable_available_count": 1},
                                  codex_details(*THREE_CREDITS), NOW)
        assert b.available == 2

    def test_only_available_rows_are_listed(self):
        b = br.parse_codex_banked(
            {"available_count": 1, "applicable_available_count": 0},
            codex_details(credit("spent", "2026-10-25T00:00:00Z", status="consumed"),
                          credit("expired", "2026-10-20T00:00:00Z", status="expired"),
                          credit("live", "2026-10-26T00:00:00Z")), NOW)
        assert [c.id for c in b.credits] == ["live"]

    def test_a_credit_past_its_expiry_is_not_listed(self):
        b = br.parse_codex_banked(
            USAGE_BLOCK, codex_details(credit("gone", "2026-10-01T00:00:00Z"),
                                       credit("live", "2026-10-26T00:00:00Z")), NOW)
        assert [c.id for c in b.credits] == ["live"]

    def test_a_count_only_response_when_the_details_call_failed(self):
        b = br.parse_codex_banked(USAGE_BLOCK, None, NOW)
        assert b.available == 3 and b.applicable == 0 and b.credits == []

    def test_details_alone_still_give_the_count(self):
        b = br.parse_codex_banked(None, codex_details(*THREE_CREDITS), NOW)
        assert b.available == 3 and b.applicable is None

    def test_total_earned_is_not_a_balance(self):
        b = br.parse_codex_banked(None, {"available_count": 1, "total_earned_count": 9,
                                         "credits": []}, NOW)
        assert b.available == 1

    def test_nothing_known_is_none(self):
        assert br.parse_codex_banked(None, None, NOW) is None
        assert br.parse_codex_banked("x", [], NOW) is None

    def test_a_list_alone_never_stands_in_for_the_count(self):
        """Without any available_count the length of the list is not a count."""
        assert br.parse_codex_banked(None, {"credits": [credit("a", "2026-10-26T00:00:00Z")]},
                                     NOW) is None

    def test_a_malformed_credit_is_skipped(self):
        b = br.parse_codex_banked(
            USAGE_BLOCK, codex_details("junk", credit("", "2026-10-26T00:00:00Z"),
                                       credit("ok", "2026-10-26T00:00:00Z")), NOW)
        assert [c.id for c in b.credits] == ["ok"]

    def test_a_credit_of_another_reset_type_is_not_listed(self):
        b = br.parse_codex_banked(
            USAGE_BLOCK, codex_details(credit("other", "2026-10-26T00:00:00Z",
                                              reset_type="something_else"),
                                       credit("ok", "2026-10-27T00:00:00Z")), NOW)
        assert [c.id for c in b.credits] == ["ok"]

    def test_zero_available_is_a_valid_empty_set(self):
        b = br.parse_codex_banked({"available_count": 0, "applicable_available_count": 0},
                                  codex_details(), NOW)
        assert b.state == "ok" and b.available == 0


class TestClaimable:
    def claude(self, **kw):
        return br.parse_claude_banked(claude_block(**kw), NOW)

    def test_a_live_claude_grant_is_claimable(self):
        claim = br.claim_for(self.claude(), live=True, now=NOW)
        assert claim.provider == "claude"
        assert claim.credit_id == "opus55-launch-promax-20260921"
        assert claim.expires_at == OCT22
        assert "five_hour" in claim.clears

    def test_cached_data_can_never_be_spent(self):
        assert br.claim_for(self.claude(), live=False, now=NOW) is None

    def test_nothing_known_is_not_claimable(self):
        assert br.claim_for(None, live=True, now=NOW) is None

    def test_an_ineligible_account_is_not_claimable(self):
        b = br.parse_claude_banked({"eligible": False, "ineligible_reason": "surface"}, NOW)
        assert br.claim_for(b, live=True, now=NOW) is None

    def test_only_the_next_grant_is_offered_even_when_another_is_usable(self):
        b = br.parse_claude_banked(claude_block(
            grant(id="first", ends_at="2026-10-15T00:00:00+00:00"),
            grant(id="second", ends_at="2026-11-15T00:00:00+00:00"),
            next_grant_id="second"), NOW)
        assert br.claim_for(b, live=True, now=NOW).credit_id == "second"

    def test_a_next_grant_id_naming_no_known_grant_is_not_claimable(self):
        assert br.claim_for(self.claude(next_grant_id="ghost"), live=True, now=NOW) is None
        assert br.claim_for(self.claude(next_grant_id=None), live=True, now=NOW) is None

    def test_a_paused_next_grant_is_not_claimable(self):
        b = br.parse_claude_banked(claude_block(grant(paused=True)), NOW)
        assert br.claim_for(b, live=True, now=NOW) is None

    def test_a_grant_that_is_not_usable_now_is_not_claimable(self):
        b = br.parse_claude_banked(claude_block(grant(usable_now=False)), NOW)
        assert br.claim_for(b, live=True, now=NOW) is None

    def test_an_expired_grant_is_not_claimable(self):
        b = br.parse_claude_banked(claude_block(grant(ends_at="2026-10-12T18:00:00+00:00")), NOW)
        assert br.claim_for(b, live=True, now=NOW) is None

    def test_a_cooldown_in_the_future_blocks_and_one_in_the_past_does_not(self):
        future = self.claude(cooldown_until="2026-10-12T19:00:00+00:00")
        past = self.claude(cooldown_until="2026-10-12T18:00:00+00:00")
        assert br.claim_for(future, live=True, now=NOW) is None
        assert br.claim_for(past, live=True, now=NOW) is not None

    def test_a_grant_that_needs_a_limit_waits_for_one(self):
        needs = grant(use_requires_limit=True)
        below = br.parse_claude_banked(claude_block(needs, at_limit=False), NOW)
        at = br.parse_claude_banked(claude_block(needs, at_limit=True), NOW)
        assert br.claim_for(below, live=True, now=NOW) is None
        assert br.claim_for(at, live=True, now=NOW) is not None

    def test_codex_needs_something_to_reset_right_now(self):
        none_now = br.parse_codex_banked(USAGE_BLOCK, codex_details(*THREE_CREDITS), NOW)
        some = br.parse_codex_banked({"available_count": 3, "applicable_available_count": 1},
                                     codex_details(*THREE_CREDITS), NOW)
        assert br.claim_for(none_now, live=True, now=NOW) is None
        claim = br.claim_for(some, live=True, now=NOW)
        assert claim.provider == "codex"
        assert claim.credit_id == "c-oct22"              # the one that expires first
        assert claim.expires_at == datetime(2026, 10, 22, 9, 0, tzinfo=UTC)

    def test_codex_without_details_is_claimable_with_no_credit_id(self):
        b = br.parse_codex_banked({"available_count": 1, "applicable_available_count": 1},
                                  None, NOW)
        claim = br.claim_for(b, live=True, now=NOW)
        assert claim is not None and claim.credit_id is None

    def test_codex_with_an_unknown_applicable_count_is_not_claimable(self):
        b = br.parse_codex_banked(None, codex_details(*THREE_CREDITS), NOW)
        assert br.claim_for(b, live=True, now=NOW) is None

    def test_codex_cached_data_can_never_be_spent(self):
        b = br.parse_codex_banked({"available_count": 3, "applicable_available_count": 3},
                                  codex_details(*THREE_CREDITS), NOW)
        assert br.claim_for(b, live=False, now=NOW) is None


class TestHints:
    def test_codex_with_nothing_to_reset(self):
        b = br.parse_codex_banked(USAGE_BLOCK, codex_details(*THREE_CREDITS), NOW)
        assert br.claim_hint(b, NOW) == "nothing to reset now"

    def test_claude_cooling_down(self):
        b = br.parse_claude_banked(
            claude_block(cooldown_until="2026-10-12T19:00:00+00:00"), NOW)
        assert br.claim_hint(b, NOW) == "cooling down"

    def test_claude_paused(self):
        b = br.parse_claude_banked(claude_block(
            grant(id="held", paused=True), grant(id="other", ends_at="2026-11-01T00:00:00+00:00"),
            next_grant_id="held"), NOW)
        assert b.available == 1
        assert br.claim_hint(b, NOW) == "paused"

    def test_no_hint_when_nothing_is_available(self):
        b = br.parse_claude_banked(claude_block(grant(paused=True)), NOW)
        assert br.claim_hint(b, NOW) == ""

    def test_claude_waiting_for_a_limit(self):
        b = br.parse_claude_banked(claude_block(grant(use_requires_limit=True,
                                                      usable_now=False)), NOW)
        assert br.claim_hint(b, NOW) == "usable once limited"

    def test_a_claimable_reset_needs_no_hint(self):
        b = br.parse_claude_banked(claude_block(), NOW)
        assert br.claim_hint(b, NOW) == ""


class TestSummary:
    TZ = timezone.utc

    def test_count_and_next_expiry_with_days_left(self):
        b = br.parse_codex_banked(USAGE_BLOCK, codex_details(*THREE_CREDITS), NOW)
        text, urgent = br.summary_text(b, NOW, tz=self.TZ)
        assert text == "3 available · next expires Oct 22 (10 d)"
        assert urgent is False

    def test_within_a_week_is_urgent(self):
        now = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)
        b = br.parse_codex_banked(USAGE_BLOCK, codex_details(*THREE_CREDITS), now)
        text, urgent = br.summary_text(b, now, tz=self.TZ)
        assert text == "3 available · next expires Oct 22 (4 d)"
        assert urgent is True

    def test_exactly_seven_days_is_urgent(self):
        now = OCT22 - timedelta(days=7)
        b = br.parse_claude_banked(claude_block(), now)
        assert br.summary_text(b, now, tz=self.TZ)[1] is True

    def test_less_than_a_day_counts_hours(self):
        now = OCT22 - timedelta(hours=5)
        b = br.parse_claude_banked(claude_block(), now)
        assert br.summary_text(b, now, tz=self.TZ)[0] == "1 available · next expires Oct 22 (5 h)"

    def test_a_count_with_no_known_expiry(self):
        b = br.parse_codex_banked(USAGE_BLOCK, None, NOW)
        assert br.summary_text(b, NOW, tz=self.TZ) == ("3 available", False)

    def test_the_date_is_shown_in_the_requested_zone(self):
        b = br.parse_claude_banked(claude_block(grant(ends_at="2026-10-22T23:30:00+00:00")), NOW)
        plus3 = timezone(timedelta(hours=3))
        assert "Oct 23" in br.summary_text(b, NOW, tz=plus3)[0]
        assert "Oct 22" in br.summary_text(b, NOW, tz=UTC)[0]

    def test_expiry_ignores_spent_paused_and_expired_credits(self):
        b = br.parse_claude_banked(claude_block(
            grant(id="paused", paused=True, ends_at="2026-10-13T00:00:00+00:00"),
            grant(id="old", ends_at="2026-10-01T00:00:00+00:00"),
            grant(id="live", ends_at="2026-11-01T00:00:00+00:00")), NOW)
        assert br.next_expiry(b, NOW) == datetime(2026, 11, 1, tzinfo=UTC)


class TestWouldRestore:
    def test_a_credit_valid_at_the_run_out_restores_it(self):
        b = br.parse_claude_banked(claude_block(), NOW)
        assert br.restores_at(b, OCT22 - timedelta(hours=1), NOW) is True

    def test_a_credit_that_expires_first_does_not(self):
        b = br.parse_claude_banked(claude_block(), NOW)
        assert br.restores_at(b, OCT22 + timedelta(hours=1), NOW) is False

    def test_a_later_credit_is_enough(self):
        b = br.parse_codex_banked(USAGE_BLOCK, codex_details(*THREE_CREDITS), NOW)
        assert br.restores_at(b, datetime(2026, 11, 1, tzinfo=UTC), NOW) is True

    def test_a_paused_credit_does_not(self):
        b = br.parse_claude_banked(claude_block(grant(paused=True)), NOW)
        assert br.restores_at(b, NOW + timedelta(hours=1), NOW) is False

    def test_a_count_with_no_expiry_data_is_assumed_good(self):
        b = br.parse_codex_banked(USAGE_BLOCK, None, NOW)
        assert br.restores_at(b, NOW + timedelta(days=30), NOW) is True

    def test_nothing_banked_restores_nothing(self):
        assert br.restores_at(None, NOW, NOW) is False
        empty = br.parse_codex_banked({"available_count": 0}, codex_details(), NOW)
        assert br.restores_at(empty, NOW, NOW) is False


class TestCacheRoundTrip:
    """The snapshot cache is JSON on disk, so a BankedResets must survive it."""

    def roundtrip(self, banked):
        import json
        from dataclasses import asdict
        raw = json.dumps(asdict(banked), default=lambda o: o.isoformat())
        return br.from_dict(json.loads(raw))

    def test_claude(self):
        b = br.parse_claude_banked(claude_block(cooldown_until="2026-10-12T19:00:00+00:00"), NOW)
        assert self.roundtrip(b) == b

    def test_codex(self):
        b = br.parse_codex_banked(USAGE_BLOCK, codex_details(*THREE_CREDITS), NOW)
        assert self.roundtrip(b) == b

    def test_ineligible(self):
        b = br.parse_claude_banked({"eligible": False, "ineligible_reason": "surface"}, NOW)
        assert self.roundtrip(b) == b

    @pytest.mark.parametrize("bad", [None, "x", 5, [], {}, {"provider": "mars"}])
    def test_garbage_is_none(self, bad):
        assert br.from_dict(bad) is None
