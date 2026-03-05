# Leadership 360 Feedback Application

A secure, role-based Flask application for running a leadership 360 program with:

- Likert feedback (11 statements, 1-4 scale)
- BWS feedback (4 sets, choose one Most and one Least per set)
- CSV-driven relationship mapping between feedback givers and recipients
- Program Administrator controls (CSV upload, timeline, exports)

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5000`.

## Login

- Default admin credentials: `admin / admin` (override with `ADMIN_ID` and `ADMIN_PASSWORD` env vars)
- Feedback givers: first login uses Employee ID as both username and password.

## CSV input format

Required headers:

- `Recipient Employee ID`
- `Recipient Name`
- `Recipient Function Name`
- `Feedback giver Employee ID`
- `Feedback giver Name`
- `Feedback giver Class`

`Feedback giver Class` supports `Class 1` to `Class 12` as provided.

## Key behavior

- Save draft and resume later
- Submit only when all required responses are complete
- Submitted forms become non-editable
- Program end date closes feedback collection
- Admin can upload incremental CSV files during open timeline
- Two admin-only exports after end date:
  - `likert_output.xlsx`
  - `bws_output.xlsx`
