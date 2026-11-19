# Job Automation - Phase 0 + Phase 1

## First-time setup of your personal files

Your personal data is never committed (see `.gitignore`). After cloning:

```
copy profile\master_profile.example.yaml profile\master_profile.yaml
copy profile\my_answers.example.yaml profile\my_answers.yaml
copy .env.example .env
```

Then fill in `profile/master_profile.yaml` (your resume facts), `.env` (sheet ID, AI keys),
put your Google service-account JSON at `credentials/service_account.json`, and fill the
`answers:` block in `config.yaml` (created from `config.default.yaml` on the first run).


Every morning at 6 AM: collect jobs from the last 72 hours (LinkedIn, Indeed, company
career pages) across PAN India, remove every duplicate (also across days and platforms),
and write **all** distinct jobs - before any filtering - to the `RAW_DATA` tab of your
Google Sheet.

Phase 2 (below) adds the daily shortlist and tailored resume PDFs. Phases 3-5 (daily sheet
tab with status dropdown, recruiter LinkedIn URLs, auto-apply) come next.

## Phase 0 - one-time setup (about 20 minutes)

### 1. Python environment (Python 3.10 - 3.12 recommended)
```bash
cd job_automation
python -m venv .venv
# Windows:  .venv\Scripts\activate        Mac/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```
If `pip install python-jobspy` fails on Python 3.13, create the venv with 3.11 or 3.12.

### 2. Google Sheet
1. In Google Sheets create a blank spreadsheet named **Job Tracker**.
2. Copy the ID from its URL (`.../spreadsheets/d/<ID>/edit`).
3. `copy .env.example .env` (or `cp`) and paste the ID after `SHEET_ID=`.

### 3. Service account (lets the script write to the sheet without your password)
1. Open https://console.cloud.google.com and create a project (any name).
2. **APIs & Services -> Library**: enable **Google Sheets API** and **Google Drive API**.
3. **IAM & Admin -> Service Accounts -> Create service account** (any name, no roles needed).
4. Open it -> **Keys -> Add key -> Create new key -> JSON**. Save the file as
   `credentials/service_account.json` inside this project.
5. Open that JSON, copy the `client_email` value, and **share your Job Tracker sheet with
   that email as Editor**.

### 4. Verify
```bash
python run.py init-sheet        # creates RAW_DATA, DEDUPE_INDEX (hidden), CONFIG tabs
python run.py check-ats         # tells you which career-page slugs in config.yaml work
python run.py run --quick       # smoke test: 2 search terms, 10 results each
```
Open the sheet: you should see rows in `RAW_DATA`.

### 5. Schedule for 6:00 AM
**Windows (Task Scheduler)**: Create Task -> Trigger: Daily 06:00 -> Action: start program
`C:\path\to\job_automation\.venv\Scripts\python.exe` with arguments `run.py run` and
"Start in" `C:\path\to\job_automation`. In Settings tick **"Run task as soon as possible
after a scheduled start is missed"**; in Conditions tick **"Wake the computer to run this
task"**. Don't let the laptop sleep/shut down at night.

**Linux/Mac (cron)**: `crontab -e`, then
```
0 6 * * * cd /path/to/job_automation && .venv/bin/python run.py run >> logs/cron.log 2>&1
```
Cron uses the machine's clock - make sure it is set to IST.

## How it works
| Step | Where |
|---|---|
| Collect LinkedIn + Indeed (14 search terms from your target roles) | `jobbot/collectors/jobspy_collector.py` |
| Collect company career pages (Greenhouse, Lever, Ashby, SmartRecruiters APIs) | `jobbot/collectors/ats.py` |
| 72-hour window, invalid-row removal | `jobbot/pipeline.py` |
| Dedupe on platform ID, canonical URL and company+title+city | `jobbot/models.py` |
| Local SQLite memory (`data/jobs.db`) + CSV backup (`data/raw_<date>.csv`) | `jobbot/store.py` |
| Mirror to Google Sheets | `jobbot/sheets.py` |

**Failure handling**: every search query and every company board is isolated, so one block
or timeout never stops the rest. If Google Sheets is down or rate-limited, rows stay in
the local database and are sent on the next run. If you lose `data/jobs.db`, the dedupe
memory is rebuilt from the hidden `DEDUPE_INDEX` tab.

## Good to know
- **No repeats**: `RAW_DATA` only receives jobs never seen before. The same role posted on
  LinkedIn and Indeed is detected via the company+title+city key. Two genuinely different
  openings with the same title, company and city are treated as one (rare).
- **LinkedIn/Indeed blocking**: run from your home connection. A cloud VPS IP is often
  blocked. Lower `results_per_query` or raise `delay_seconds` in `config.yaml` if you see
  failures in `logs/`. Exit code 2 means nothing was collected.
- **Career-page slugs** in `config.yaml` are unverified starter guesses; use `check-ats`
  and add the companies you actually care about.
- Sheets cap at 10 million cells; at about 300 jobs/day `RAW_DATA` lasts well over a year.

## Phase 2 - daily shortlist + tailored resume PDFs

One-time setup:
```bash
pip install -r requirements.txt          # now includes playwright
playwright install chromium              # the PDF engine (one-time, ~150 MB)
```
1. **Review `profile/master_profile.yaml`.** It is the only source of facts for your resumes:
   the tailoring can reorder and rephrase it but can never add a tool, number or claim that is
   not written there. Items marked `extra_*` (JMeter, Grafana, log analysis, RCA, Jenkins,
   Linux, Core Java) are shown only when a job description asks for them. Headlines under
   `headlines:` are editable.
2. **(Optional, recommended) free AI keys** in `.env` (see `.env.example`). With none, resumes
   still build using your original wording reordered per job; with keys the summary and bullets
   are rewritten to mirror each JD. Providers rotate and fall back automatically.
3. Check the layout, then try it:
```bash
python run.py sample-resume              # writes output/sample_resume.pdf
python run.py shortlist --limit 3        # 3 jobs from what you already collected
python run.py run                        # daily job: collect -> shortlist -> resume PDFs
```
Results land in `output/<date>/`: one PDF per job (`<Your_Name>_<Company>_<Title>.pdf`) and
`shortlist_<date>.csv` (score, tier, matched/missing keywords, JD summary, apply type, links).

**How jobs are chosen** (`jobbot/scoring.py`): each unshortlisted job from the last 7 days is
scored on title fit, experience fit (vs your ~2.6 years, computed from `start_date`), skill
overlap, domain overlap and freshness. The shortlist fills **Tier 1 Core** first, then relaxes
through **Tier 2 Related**, **Tier 3 Stretch** and **Tier 4 Skills match** until it reaches 20
(`daily_target`). If fewer than 15 qualify you get a warning. A job is never shortlisted twice.
`Apply Type` = Auto for Greenhouse/Lever/Ashby/SmartRecruiters/Workable links, Manual otherwise.

## Phase 3 - daily sheet tab + recruiter contacts

`python run.py daily-tab` (also runs automatically at the end of `python run.py run`) writes
today's shortlist into a new tab of **Job Tracker** named with the date (newest tab first).
- Columns are grouped and colour-coded: Action, Job, Match, Documents, People, Yours.
- **Status** is a dropdown (New, Manual Apply Needed, Applied (Auto), Auto-Apply Failed - Apply
  Manually, Applied (Manual), Interview, Rejected, Skipped, Job Expired) with colours.
- Re-running only appends missing rows; your edits (status, notes, sorting) are never overwritten.
- **Resume File** shows the PDF's full path on your PC (browsers cannot open local links from
  Google Sheets): open `output\<date>\` and attach the file named after the company.
- Until Phase 4 exists every job is `Manual Apply Needed` (`apply.auto_enabled: false`).
- `python run.py shortlist --regen` rebuilds today's resumes (use after adding AI keys).

**Recruiter LinkedIn contacts** (optional, uses your own LinkedIn account):
1. Once: `python run.py linkedin-login`, sign in in the window that opens.
2. After that the daily run searches one recruiter per company (max 20/day, long random pauses)
   and fills `Contact` / `Contact LinkedIn`; blank if none found.
3. It stops for the day if LinkedIn shows a verification page. LinkedIn discourages automation,
   so keep `max_per_day` low. If it keeps being challenged set `people.headless: false`, or
   `people.enabled: false` to switch it off.

## Tests
```bash
python -m unittest discover -s tests -v
```

## Phase 5 - daily 6 AM schedule (v6.4)

`python run.py schedule` creates a Windows Task Scheduler job "JobBot Daily" at 06:00 that runs `python run.py run`
(collect -> shortlist + resumes -> sheet tab -> auto-apply only if `apply.auto_enabled: true`).
- Other time: `python run.py schedule --time 07:30`   Check: `python run.py schedule --status`   Remove: `--remove`
- Runs late if the PC was off at 06:00 (as soon as it is on), wakes the PC if asleep, stops after 3 hours, never starts twice.
- The PC must be on (or asleep, not shut down) and online. Logs: `logs\<date>.log`.
- If the command fails, open PowerShell as Administrator and run it again.
