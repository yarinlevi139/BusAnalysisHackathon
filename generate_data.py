"""Generate fabricated bus fare-tap data and store it in a SQLite database.

Usage:
    python generate_data.py   # writes data/bus.db (overwrites an existing file)

All people, card numbers and coordinates are invented. Only the standard library is used.
"""
import bisect
import itertools
import math
import random
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

SEED = 42
START_DATE = date(2026, 10, 1)
DAYS = 45
NUM_CARDS = 6000
NUM_STOPS = 60
STOPS_PER_LINE = (14, 20)
TRANSFER_WINDOW = timedelta(minutes=90)
DB_PATH = Path(__file__).parent / "data" / "bus.db"

CENTER_LAT, CENTER_LON = 32.0853, 34.7818
NEIGHBORHOODS = ["Central", "North", "South", "Old Port", "University",
                 "Beach", "Market", "Industrial", "Hills", "Riverside"]
LINES = [
    (4, "Beach Express"), (5, "Central Loop"), (18, "University Shuttle"),
    (25, "Market Crosstown"), (42, "Hills Route"), (61, "Riverside"),
    (74, "Industrial Connector"), (84, "North Line"),
]
CARD_MIX = {"adult": 0.6, "student": 0.2, "senior": 0.1, "occasional": 0.1}
FARES = {"adult": 6.5, "student": 3.25, "senior": 3.5, "occasional": 9.0}
WEEKEND_FACTOR = {4: 0.6, 5: 0.7}  # Friday, Saturday

rng = random.Random(SEED)


def hour_factor(hour: int) -> float:
    if 7 <= hour < 9 or 16 <= hour < 19:
        return 1.8
    if 5 <= hour < 22:
        return 0.8
    return 0.3


def headway_minutes(hour: int) -> int:
    if 7 <= hour < 9 or 16 <= hour < 19:
        return 8
    if 5 <= hour < 22:
        return 15
    return 30


def make_stops() -> list[dict]:
    stops = []
    for stop_id in range(1, NUM_STOPS + 1):
        zone = rng.choice(NEIGHBORHOODS)
        stops.append({
            "stop_id": stop_id,
            "name": f"{zone} {stop_id:02d}",
            "zone": zone,
            "lat": CENTER_LAT + rng.uniform(-0.04, 0.04),
            "lon": CENTER_LON + rng.uniform(-0.04, 0.04),
            "weight": rng.uniform(0.3, 1.6),
        })
    return stops


def make_route(stops: list[dict], length: int) -> list[dict]:
    """Chain nearby stops together so each line looks like a plausible corridor."""
    current = rng.choice(stops)
    route = [current]
    remaining = [s for s in stops if s is not current]
    while len(route) < length:
        nearest = sorted(remaining, key=lambda s: (s["lat"] - current["lat"]) ** 2
                         + (s["lon"] - current["lon"]) ** 2)[:3]
        current = rng.choice(nearest)
        route.append(current)
        remaining.remove(current)
    return route


def make_cards() -> tuple[list[dict], list[float]]:
    types = rng.choices(list(CARD_MIX), weights=CARD_MIX.values(), k=NUM_CARDS)
    cards = []
    for card_type in types:
        cards.append({
            "card_number": f"7{rng.randint(10**14, 10**15 - 1)}",
            "card_type": card_type,
            "usage": rng.paretovariate(1.8),  # a few heavy riders, many light ones
        })
    cumulative = list(itertools.accumulate(c["usage"] for c in cards))
    return cards, cumulative


def make_weather() -> dict[date, dict]:
    weather = {}
    for offset in range(DAYS):
        day = START_DATE + timedelta(days=offset)
        rainy = rng.random() < 0.15
        weather[day] = {
            "condition": "rain" if rainy else rng.choice(["sunny", "cloudy"]),
            "temperature_c": round(rng.uniform(16, 31), 1),
            "rain_mm": round(rng.uniform(2, 25), 1) if rainy else 0.0,
        }
    return weather


def build_boardings(lines, cards, cumulative, weather):
    rows = []
    for offset in range(DAYS):
        day = START_DATE + timedelta(days=offset)
        weekend = WEEKEND_FACTOR.get(day.weekday(), 1.0)
        rain = 1.25 if weather[day]["rain_mm"] > 0 else 1.0
        for line in lines:
            departure = datetime.combine(day, time(5, 30))
            last_departure = datetime.combine(day, time(23, 30))
            trip_no = 0
            while departure <= last_departure:
                trip_no += 1
                outbound = trip_no % 2 == 1
                route = line["route"] if outbound else list(reversed(line["route"]))
                direction = "outbound" if outbound else "inbound"
                trip_id = f"{day:%Y%m%d}-{line['line_number']}-{trip_no:03d}"
                vehicle_id = f"BUS-{line['line_number']}-{rng.randint(1, 12):02d}"
                demand = line["popularity"] * hour_factor(departure.hour) * weekend * rain

                for position, stop in enumerate(route):
                    mean = demand * stop["weight"] * 0.6
                    boardings = max(0, round(rng.gauss(mean, math.sqrt(mean) + 0.01)))
                    for _ in range(boardings):
                        card = cards[bisect.bisect(cumulative, rng.random() * cumulative[-1])]
                        boarded_at = (departure
                                      + timedelta(minutes=position * line["minutes_per_segment"])
                                      + timedelta(seconds=rng.randint(0, 59)))
                        rows.append({
                            "card_number": card["card_number"],
                            "card_type": card["card_type"],
                            "line_number": line["line_number"],
                            "trip_id": trip_id,
                            "vehicle_id": vehicle_id,
                            "direction": direction,
                            "stop_id": stop["stop_id"],
                            "boarded_at": boarded_at,
                            "gps_lat": round(stop["lat"] + rng.gauss(0, 0.0002), 6),
                            "gps_lon": round(stop["lon"] + rng.gauss(0, 0.0002), 6),
                            "payment_method": ("credit_contactless" if card["card_type"] == "occasional"
                                               else rng.choices(["stored_value", "mobile_app"],
                                                                weights=[0.6, 0.4])[0]),
                        })
                departure += timedelta(minutes=headway_minutes(departure.hour))
    return rows


def mark_transfers(rows: list[dict]) -> None:
    """A boarding is a free transfer if the same card boarded a different line within the window."""
    last_seen: dict[str, dict] = {}
    for row in sorted(rows, key=lambda r: (r["card_number"], r["boarded_at"])):
        previous = last_seen.get(row["card_number"])
        is_transfer = (previous is not None
                       and row["boarded_at"] - previous["boarded_at"] <= TRANSFER_WINDOW
                       and previous["line_number"] != row["line_number"])
        row["is_transfer"] = int(is_transfer)
        row["fare_paid"] = 0.0 if is_transfer else FARES[row["card_type"]]
        last_seen[row["card_number"]] = row


def write_database(lines, stops, cards, weather, rows) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE lines (
            line_number INTEGER PRIMARY KEY,
            line_name TEXT NOT NULL,
            stop_count INTEGER NOT NULL
        );
        CREATE TABLE stops (
            stop_id INTEGER PRIMARY KEY,
            stop_name TEXT NOT NULL,
            zone TEXT NOT NULL,
            latitude REAL NOT NULL,
            longitude REAL NOT NULL
        );
        CREATE TABLE line_stops (
            line_number INTEGER REFERENCES lines(line_number),
            stop_sequence INTEGER NOT NULL,
            stop_id INTEGER REFERENCES stops(stop_id),
            PRIMARY KEY (line_number, stop_sequence)
        );
        CREATE TABLE cards (
            card_number TEXT PRIMARY KEY,
            card_type TEXT NOT NULL CHECK (card_type IN ('adult', 'student', 'senior', 'occasional'))
        );
        CREATE TABLE daily_weather (
            date TEXT PRIMARY KEY,
            condition TEXT NOT NULL,
            temperature_c REAL NOT NULL,
            rain_mm REAL NOT NULL
        );
        CREATE TABLE boardings (
            boarding_id INTEGER PRIMARY KEY,
            card_number TEXT NOT NULL REFERENCES cards(card_number),
            line_number INTEGER NOT NULL REFERENCES lines(line_number),
            trip_id TEXT NOT NULL,
            vehicle_id TEXT NOT NULL,
            direction TEXT NOT NULL CHECK (direction IN ('outbound', 'inbound')),
            stop_id INTEGER NOT NULL REFERENCES stops(stop_id),
            boarded_at TEXT NOT NULL,            -- 'YYYY-MM-DD HH:MM:SS', local time
            gps_latitude REAL NOT NULL,
            gps_longitude REAL NOT NULL,
            payment_method TEXT NOT NULL,
            fare_paid REAL NOT NULL,
            is_transfer INTEGER NOT NULL CHECK (is_transfer IN (0, 1))
        );
    """)
    conn.executemany("INSERT INTO lines VALUES (?, ?, ?)",
                     [(l["line_number"], l["line_name"], len(l["route"])) for l in lines])
    conn.executemany("INSERT INTO stops VALUES (?, ?, ?, ?, ?)",
                     [(s["stop_id"], s["name"], s["zone"], round(s["lat"], 6), round(s["lon"], 6))
                      for s in stops])
    conn.executemany("INSERT INTO line_stops VALUES (?, ?, ?)",
                     [(l["line_number"], seq, s["stop_id"])
                      for l in lines for seq, s in enumerate(l["route"], start=1)])
    conn.executemany("INSERT INTO cards VALUES (?, ?)",
                     [(c["card_number"], c["card_type"]) for c in cards])
    conn.executemany("INSERT INTO daily_weather VALUES (?, ?, ?, ?)",
                     [(d.isoformat(), w["condition"], w["temperature_c"], w["rain_mm"])
                      for d, w in weather.items()])
    conn.executemany(
        """INSERT INTO boardings (card_number, line_number, trip_id, vehicle_id, direction, stop_id,
               boarded_at, gps_latitude, gps_longitude, payment_method, fare_paid, is_transfer)
           VALUES (:card_number, :line_number, :trip_id, :vehicle_id, :direction, :stop_id,
               :boarded_at, :gps_lat, :gps_lon, :payment_method, :fare_paid, :is_transfer)""",
        [{**r, "boarded_at": r["boarded_at"].strftime("%Y-%m-%d %H:%M:%S")}
         for r in sorted(rows, key=lambda r: r["boarded_at"])],
    )
    conn.executescript("""
        CREATE INDEX idx_boardings_time ON boardings(boarded_at);
        CREATE INDEX idx_boardings_line ON boardings(line_number);
        CREATE INDEX idx_boardings_stop ON boardings(stop_id);
        CREATE INDEX idx_boardings_card ON boardings(card_number);
    """)
    conn.commit()
    conn.close()


def main() -> None:
    stops = make_stops()
    lines = []
    for line_number, line_name in LINES:
        route = make_route(stops, rng.randint(*STOPS_PER_LINE))
        lines.append({
            "line_number": line_number,
            "line_name": line_name,
            "route": route,
            "popularity": rng.uniform(0.5, 1.5),
            "minutes_per_segment": rng.uniform(2, 4),
        })
    cards, cumulative = make_cards()
    weather = make_weather()
    rows = build_boardings(lines, cards, cumulative, weather)
    mark_transfers(rows)
    write_database(lines, stops, cards, weather, rows)
    print(f"Wrote {len(rows):,} boardings for {len(cards):,} cards to {DB_PATH}")


if __name__ == "__main__":
    main()
