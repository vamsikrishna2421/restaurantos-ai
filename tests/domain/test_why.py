"""Pass 15: the why behind a caution (domain.why) — pure cause-picking over facts a check already
computed. Every expectation is the evidence bar doing its work: a cause is named only when its
numbers clear it, two causes stack biggest first, and a wait incident with nothing standing out
gets the explicit no-single-cause line instead of a guessed reason.
"""

from __future__ import annotations

from restaurantos.domain.why import StationWindow, capacity_why, stock_why, wait_incident_why

# ----- capacity -----------------------------------------------------------------------------------------------


def test_capacity_why_names_an_unfilled_slot_for_tomorrow():
    why = capacity_why(station_name="Curry", daypart_label="Dinner", day_name="Sunday",
                       expected_tickets=40, cooks=1, slots=3, capacity_tickets=24,
                       full_capacity_tickets=72, usual_basis=True,
                       typical_tickets=None, typical_days=0)
    assert why is not None
    assert "Only 1 of the station's 3 cooking slots have a usual-job cook" in why
    assert "72 tickets, not 24" in why


def test_capacity_why_names_a_surge_against_the_same_weekday_norm():
    why = capacity_why(station_name="Biryani", daypart_label="Dinner", day_name="Saturday",
                       expected_tickets=50, cooks=2, slots=2, capacity_tickets=48,
                       full_capacity_tickets=48, usual_basis=False,
                       typical_tickets=30.0, typical_days=3)
    assert why is not None
    assert "forecast runs hot" in why and "50 tickets" in why and "~30" in why and "Saturday" in why


def test_capacity_why_stacks_two_causes_biggest_first():
    # surge excess is 20 tickets; the missing cook costs 24 — staffing leads
    why = capacity_why(station_name="Curry", daypart_label="Dinner", day_name="Sunday",
                       expected_tickets=50, cooks=1, slots=2, capacity_tickets=24,
                       full_capacity_tickets=48, usual_basis=True,
                       typical_tickets=30.0, typical_days=4)
    assert why is not None and why.startswith("Two things stack up:")
    assert why.index("cooking slots") < why.index("forecast runs hot")


def test_capacity_why_small_wiggles_clear_no_bar():
    # expected 10% over a thin norm and a full crew: nothing stands out, and with a norm on record
    # the honest answer is the no-single-cause line, not silence and not a guess
    why = capacity_why(station_name="Fry", daypart_label="Lunch", day_name="Monday",
                       expected_tickets=22, cooks=1, slots=1, capacity_tickets=21,
                       full_capacity_tickets=21, usual_basis=False,
                       typical_tickets=20.0, typical_days=3)
    assert why is not None and why.startswith("No single cause stands out")
    assert "in line with a typical Monday Lunch (about 20 tickets)" in why


def test_capacity_why_without_history_or_a_gap_is_null():
    # no same-weekday record and the crew fills the slots: nothing honest to say
    assert capacity_why(station_name="Fry", daypart_label="Lunch", day_name="Monday",
                        expected_tickets=22, cooks=1, slots=1, capacity_tickets=21,
                        full_capacity_tickets=21, usual_basis=False,
                        typical_tickets=None, typical_days=0) is None
    # nobody on the station at all: the card's own detail already says exactly that
    assert capacity_why(station_name="Biryani", daypart_label="Dinner", day_name="Sunday",
                        expected_tickets=40, cooks=0, slots=2, capacity_tickets=0,
                        full_capacity_tickets=48, usual_basis=True,
                        typical_tickets=None, typical_days=0) is None


# ----- stock --------------------------------------------------------------------------------------------------


def test_stock_why_names_the_dish_doing_the_burning():
    why = stock_why(projected_need=5000, unit="g",
                    dishes=[("Paneer Butter Masala", 30, 3000), ("Palak Paneer", 10, 1000),
                            ("Paneer Tikka", 4, 1000)],
                    recent_daily=None, prior_daily=None)
    assert why is not None
    assert "Paneer Butter Masala" in why and "3000 of the 5000 g" in why and "30 servings" in why


def test_stock_why_names_a_usage_climb():
    why = stock_why(projected_need=900, unit="g",
                    dishes=[("Chicken Dum Biryani", 12, 300)],
                    recent_daily=900.0, prior_daily=500.0)
    assert why is not None and "Use has climbed" in why and "900" in why and "500" in why


def test_stock_why_even_spread_and_flat_use_is_null():
    # no dish reaches two fifths of the burn and use is flat: the card keeps no why line
    assert stock_why(projected_need=1000, unit="g",
                     dishes=[("A", 5, 300), ("B", 5, 300), ("C", 5, 350)],
                     recent_daily=100.0, prior_daily=100.0) is None
    assert stock_why(projected_need=0, unit="g", dishes=[], recent_daily=None,
                     prior_daily=None) is None


# ----- the slow order -----------------------------------------------------------------------------------------


def test_wait_why_points_at_the_over_capacity_station_with_its_numbers():
    legs = [StationWindow(name="Curry", tickets=20, cooks=1, slots=3, leg_min=38.0),
            StationWindow(name="Tandoor", tickets=4, cooks=1, slots=1, leg_min=6.0)]
    why = wait_incident_why(legs, window_min=45)
    assert why.startswith("The wait built at Curry (38 min of it)")
    assert "20 tickets" in why and "1 cook(s)" in why
    assert "1 of its 3 cooking slots" in why  # the second evidenced cause stacks behind the first


def test_wait_why_names_a_room_wide_surge():
    legs = [StationWindow(name="Curry", tickets=5, cooks=2, slots=2, leg_min=30.0)]
    why = wait_incident_why(legs, window_min=40, orders_in_window=52, typical_orders=30)
    assert "Curry" in why and "52 orders" in why and "typical 30" in why


def test_wait_why_names_an_86_as_a_circumstance():
    legs = [StationWindow(name="Curry", tickets=5, cooks=2, slots=2, leg_min=30.0)]
    why = wait_incident_why(legs, window_min=40, eighty_sixed=["Paneer Butter Masala"])
    assert "Paneer Butter Masala had been 86'd" in why


def test_wait_why_with_no_standout_cause_says_so():
    legs = [StationWindow(name="Curry", tickets=3, cooks=3, slots=3, leg_min=20.0),
            StationWindow(name="Tandoor", tickets=2, cooks=1, slots=1, leg_min=9.0)]
    why = wait_incident_why(legs, window_min=45, orders_in_window=10, typical_orders=11)
    assert why.startswith("No single cause stands out in the record")
    assert "no cooking slot stood empty" in why
    assert "accumulated across the whole ticket" in why


def test_wait_why_without_station_timing_says_so():
    why = wait_incident_why([], window_min=45)
    assert why.startswith("No single cause stands out in the record")
