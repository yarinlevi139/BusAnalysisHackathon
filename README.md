# Bus Insights

A fabricated bus fare-tap dataset in SQLite, plus a Strands agent that answers questions about it by writing SQL.

## Data

`python generate_data.py` writes `data/bus.db` (45 days, 8 lines, 60 stops, 6,000 cards). Every card number, rider, and GPS point is invented.

| Table | Contents |
| --- | --- |
| `boardings` | One row per tap: card, line, trip, vehicle, direction, stop, `boarded_at`, GPS lat/lon, payment method, fare, transfer flag |
| `cards` | Card number and rider type (adult, student, senior, occasional) |
| `lines` / `stops` / `line_stops` | Bus lines, stop locations and zones, and each line's stop order |
| `daily_weather` | Daily condition, temperature, and rain (rain raises ridership) |

## Run

```bash
pip install strands-agents strands-agents-tools boto3 streamlit pandas
python generate_data.py
export AWS_BEARER_TOKEN_BEDROCK="<your Bedrock API key>"   # same terminal as the next step
streamlit run app.py
```

The app opens at http://localhost:8501 with two tabs:

- **Chat**: ask questions in plain English. Each answer has an **SQL queries** expander showing the SQL the model ran, and a **Show data** toggle that displays the rows it returned (capped at 100).
- **SQL console**: run your own read-only SQL against `data/bus.db`.

Example questions: "Which stop has the most boardings on weekday mornings?", "How much do rainy days change ridership?", "What share of fares are free transfers, by rider type?"

The SQL connection is read-only. You can also open `data/bus.db` in any SQLite client. `agent.py` can still be imported on its own; it exposes `ask()` and `check_setup()`.
