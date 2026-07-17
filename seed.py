"""Seed the OpsBrain Airtable base with the demo roster and log entries.

Usage:  python seed.py          (refuses to run if Log Entries already has rows)
        python seed.py --force  (seeds anyway)

Timestamps are relative to "now" so the brief demo always has fresh entries.
Review/edit the ENTRIES text below for authenticity before the demo.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests
from dotenv import load_dotenv

load_dotenv()
AT_KEY = os.environ["AIRTABLE_API_KEY"]
AT_BASE = os.environ["AIRTABLE_BASE_ID"]
AT_LOG = os.environ.get("AIRTABLE_LOG_TABLE", "Log Entries")
AT_ROSTER = os.environ.get("AIRTABLE_ROSTER_TABLE", "Roster")
HEADERS = {"Authorization": f"Bearer {AT_KEY}", "Content-Type": "application/json"}
DEMO_PHONE = "+16026962234"

ROSTER = [
    {"phone_number": DEMO_PHONE, "name": "Demo phone (you)",
     "role": "on-duty lead", "active": True},
]

# (hours_ago, category, urgency, summary, the_ask, stated_impact, raw)
ENTRIES = [
    # --- the visible pattern: pallet jack #2 at dock door 4, three times ---
    (220, "Warehouse & Equipment", "medium",
     "Pallet jack #2 at dock door 4 wouldn't lift a full pallet this morning.",
     "Have maintenance look at pallet jack #2 before the next produce delivery.",
     None,
     "pallet jack 2 at dock 4 wouldn't lift a full pallet this morning, had to drag the manual one over from repack"),
    (120, "Warehouse & Equipment", "medium",
     "Pallet jack #2 at dock door 4 died again mid-unload and had to be swapped out.",
     "Get pallet jack #2 serviced or replaced — second failure this week.",
     "second failure this week",
     "jack 2 at door 4 quit again halfway through the reefer unload. second time this week. borrowed receiving's spare"),
    (5, "Warehouse & Equipment", "high",
     "Pallet jack #2 at dock door 4 is fully dead ahead of Saturday agency pickups.",
     "Pull the spare from receiving or rent one before Saturday 7am pickups.",
     "third failure this week",
     "pallet jack 2 dock 4 is dead for good this time, third time this week. agency pickups start 7am saturday, we need the spare from receiving or a rental"),
    # --- today-ish, so the brief has content ---
    (3, "Food Quality & Produce", "medium",
     "Two pallets of donated stone fruit arrived heavily bruised; roughly half looks salvageable.",
     "Prioritize the stone fruit on the repack line Monday before it turns.",
     "2 pallets, about half salvageable",
     "two pallets of stone fruit from the produce donor came in pretty bruised. maybe half is good. repack should hit it monday first thing"),
    (8, "Safety", "high",
     "The dock plate at door 2 isn't locking and moved underfoot during unloading.",
     "Tag door 2 out of service and get the dock plate repaired.",
     None,
     "dock plate at door 2 isn't locking down, it shifted while we were walking a pallet over it. someone's going to roll an ankle. needs to be tagged out"),
    (10, "Warehouse & Equipment", "medium",
     "Pallet wrap is running low in the warehouse — about two days of stock left.",
     "Reorder pallet wrap today.",
     "about 2 days left",
     "we're down to like two days of pallet wrap. someone needs to reorder today"),
    (20, "Dock & Receiving", "low",
     "A mixed pallet from the weekend food drive arrived unlabeled and needs sorting.",
     "Sort and label the food-drive pallet before Thursday's inventory count.",
     None,
     "got a mixed unlabeled pallet from the food drive. needs sorting before thursday's count or it'll throw the numbers off"),
    # --- older background entries ---
    (50, "Facilities", "high",
     "The walk-in freezer #1 door seal is icing up and the door isn't sealing fully.",
     "Open a facilities work order for the freezer #1 door seal.",
     None,
     "freezer 1 door seal is icing up again, door isn't shutting flush. we'll lose temp if it keeps up. needs a work order"),
    (75, "Agency & Distribution", "medium",
     "The Friday 8am pickup slot agency has missed two Fridays in a row.",
     "Call the agency to confirm or re-slot the Friday 8am pickup.",
     "2 missed pickups",
     "friday 8am slot no-showed again, that's two fridays in a row. that food sat staged all morning. someone should call them and re-slot it"),
    (80, "Dock & Receiving", "medium",
     "Friday's USDA load arrived without the reefer trailer temperature log.",
     "Add the temp log check to the driver receiving checklist.",
     None,
     "the usda load friday came in with no temp log from the reefer. we accepted it anyway. drivers should be checking for it at the door"),
    (100, "Warehouse & Equipment", "low",
     "The second plug at the forklift charger bay is dead.",
     "Have an electrician check the second charger plug.",
     None,
     "second plug on the forklift charger bay isn't charging. we're rotating on one plug"),
    (130, "Facilities", "low",
     "The cardboard baler keeps jamming when fed wet cardboard.",
     "Remind crews to keep wet cardboard out of the baler.",
     None,
     "baler jammed twice today, both times wet cardboard from the produce boxes. crews need to know to toss the wet stuff"),
    (150, "Food Quality & Produce", "medium",
     "The produce cooler door was propped open during the lunch break.",
     "Post signage and remind staff not to prop the produce cooler door.",
     None,
     "produce cooler door was propped open most of lunch. temp crept up a couple degrees. we need a sign on that door"),
    (170, "Warehouse & Equipment", "medium",
     "The liftgate on truck 7 cycles very slowly and is delaying agency route loading.",
     "Schedule a maintenance check for truck 7's liftgate.",
     None,
     "liftgate on truck 7 is taking forever to cycle, added a good 20 minutes to loading the route this morning. maintenance should look at it"),
    (190, "Dock & Receiving", "low",
     "Salvage barrels at receiving are overflowing ahead of Monday's pickup.",
     "Ask for an extra salvage barrel swap before Monday.",
     None,
     "salvage barrels at receiving are past full and monday's the next swap. we need an extra pickup or more barrels"),
    (200, "Warehouse & Equipment", "low",
     "The repack line is short on tape guns for the Saturday volunteer shift.",
     "Order six more tape guns before Saturday's volunteer shift.",
     "6 tape guns short",
     "repack is short six tape guns for the saturday volunteer group. big group coming in. order more this week"),
    (230, "Agency & Distribution", "low",
     "Thursday's repack needs two extra staging tables for a new volunteer group.",
     "Set out two extra staging tables Wednesday night.",
     "2 extra tables",
     "new volunteer group thursday, repack will need two more staging tables set up the night before"),
]


def url(table):
    return f"https://api.airtable.com/v0/{AT_BASE}/{quote(table)}"


def count(table):
    r = requests.get(url(table), headers=HEADERS, params={"maxRecords": 1}, timeout=15)
    r.raise_for_status()
    return len(r.json()["records"])


def create(table, records):
    for i in range(0, len(records), 10):
        batch = {"records": [{"fields": f} for f in records[i:i + 10]],
                 "typecast": True}
        r = requests.post(url(table), headers=HEADERS, json=batch, timeout=20)
        r.raise_for_status()


def main():
    if count(AT_LOG) and "--force" not in sys.argv:
        sys.exit(f"'{AT_LOG}' already has records — rerun with --force to add anyway.")
    now = datetime.now(timezone.utc)
    entries = []
    for hours_ago, cat, urg, summary, ask, impact, raw in ENTRIES:
        f = {
            "timestamp": (now - timedelta(hours=hours_ago)).isoformat(),
            "sender_phone": DEMO_PHONE,
            "raw_transcript": raw,
            "summary": summary,
            "category": cat,
            "urgency": urg,
            "the_ask": ask,
            "status": "confirmed",
            "sensitive_flag": False,
        }
        if impact:
            f["stated_impact"] = impact
        entries.append(f)
    create(AT_LOG, entries)
    if count(AT_ROSTER) == 0:
        create(AT_ROSTER, ROSTER)
        print(f"Seeded roster: {len(ROSTER)} number(s)")
    print(f"Seeded {len(entries)} log entries into '{AT_LOG}'.")


if __name__ == "__main__":
    main()
