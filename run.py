#!/usr/bin/env python3
"""Job automation CLI.

  python run.py init-sheet      create/format the Google Sheet tabs (run once)
  python run.py check-ats       test the company career-page slugs in config.yaml
  python run.py run             the daily job (collect -> dedupe -> RAW_DATA)
  python run.py run --quick     small smoke test (2 terms, 10 results each)
  python run.py run --no-sheets collect and store locally only
  python run.py stats           what the local store holds
  python run.py shortlist       pick the best 15-20 jobs + tailored resume PDFs (output/<date>/)
  python run.py find-boards     discover company career boards (run once, then monthly)
  python run.py apply           auto-apply dry run; add --live to really submit
  python run.py sample-resume   render a sample resume PDF to check the layout
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from jobbot.collectors.ats import AtsCollector, check_boards  # noqa: E402
from jobbot.collectors.jobspy_collector import JobSpyCollector  # noqa: E402
from jobbot.pipeline import run_pipeline  # noqa: E402
from jobbot.sheets import SheetsClient  # noqa: E402
from jobbot.store import Store  # noqa: E402


def do_shortlist(cfg: dict, store: Store, *, resumes: bool = True, use_llm: bool = True,
                 limit=None, regen: bool = False):
    """Phase 2: score + shortlist + tailored resume PDFs + local CSV."""
    from jobbot.llm import make_llm
    from jobbot.render import RenderError, ResumeRenderer
    from jobbot.shortlist import load_profile, run_shortlist

    rcfg = cfg.get("resume", {}) or {}
    now = datetime.now(timezone.utc)
    kwargs = dict(
        store=store, now=now, scfg=cfg.get("shortlist", {}), limit=limit, regen=regen,
        day=now.astimezone(get_tz(cfg.get("timezone", "Asia/Kolkata"))).strftime("%Y-%m-%d"),
        profile=load_profile(ROOT / rcfg.get("profile_file", "profile/master_profile.yaml")),
        llm=make_llm(cfg) if use_llm else None,
        out_dir=ROOT / rcfg.get("output_dir", "output"))
    if kwargs["llm"] is not None and not kwargs["llm"].available:
        logging.warning("No LLM API keys in .env - resumes use your original wording, "
                        "reordered for each job. Add a free GEMINI_API_KEY / GROQ_API_KEY "
                        "for tailored text.")
    if resumes:
        try:
            with ResumeRenderer(max_pages=int(rcfg.get("max_pages", 2))) as renderer:
                return run_shortlist(renderer=renderer, **kwargs)
        except RenderError as exc:
            logging.error("%s  Shortlisting continues without PDFs; run `python run.py "
                          "shortlist` again after fixing this.", exc)
    return run_shortlist(renderer=None, **kwargs)


def cmd_shortlist(cfg: dict, args) -> int:
    store = Store(ROOT / "data" / "jobs.db")
    rep = do_shortlist(cfg, store, resumes=not args.no_resume, use_llm=not args.no_llm,
                       limit=args.limit, regen=args.regen)
    print("\n" + rep.summary())
    return 0 if rep.total_today else 2


def cmd_sample_resume(cfg: dict, _args) -> int:
    """Render a sample PDF from the master profile (no jobs, no AI) to check the layout."""
    from jobbot.models import Job
    from jobbot.render import ResumeRenderer
    from jobbot.shortlist import load_profile
    from jobbot.tailor import tailor_resume

    rcfg = cfg.get("resume", {}) or {}
    profile = load_profile(ROOT / rcfg.get("profile_file", "profile/master_profile.yaml"))
    job = Job(source="sample", title="Product Analyst", company="Sample Co", url="https://example.com",
              description="Product Analyst for an order management SaaS platform. Jira, SQL, "
                          "user stories, PRDs, APIs, stakeholder management. 2-4 years.")
    out = ROOT / rcfg.get("output_dir", "output") / "sample_resume.pdf"
    with ResumeRenderer(max_pages=int(rcfg.get("max_pages", 2))) as r:
        pages = r.render(profile, tailor_resume(profile, job, "product", None), out)
    print(f"Sample resume written: {out}  ({pages} page(s))")
    return 0


def load_env(path: Path) -> None:
    if not path.exists():
        alt = path.with_name(".env.example")
        if not alt.exists():
            return
        print("NOTE: using .env.example - rename it to .env (the program normally reads only .env)")
        path = alt
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config() -> dict:
    path, default = ROOT / "config.yaml", ROOT / "config.default.yaml"
    if not path.exists() and default.exists():
        shutil.copyfile(default, path)
        print("config.yaml was missing - recreated it from config.default.yaml")
    if not path.exists():
        sys.exit(f"config.yaml not found in {ROOT}. Run this from the folder that contains run.py.")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def get_tz(name: str) -> timezone:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)  # type: ignore[return-value]
    except Exception:  # noqa: BLE001 - e.g. Windows without tzdata
        return timezone(timedelta(hours=5, minutes=30))


def setup_logging() -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(ROOT / "logs" / f"{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
    sh = logging.StreamHandler()
    for h in (fh, sh):
        h.setFormatter(fmt)
        root.addHandler(h)


def make_sheets(cfg: dict) -> SheetsClient:
    s = cfg["sheet"]
    client = SheetsClient(
        credentials_file=str(ROOT / s["credentials_file"]),
        spreadsheet_id=os.environ.get(s.get("spreadsheet_id_env", "SHEET_ID"), "").strip(),
        spreadsheet_name=s.get("spreadsheet_name", ""))
    client.connect()
    return client


def cmd_init_sheet(cfg: dict, _args) -> int:
    client = make_sheets(cfg)
    client.ensure_layout(cfg.get("answers"))
    print("Sheet is ready: RAW_DATA, DEDUPE_INDEX (hidden), CONFIG.")
    return 0


def load_ats_boards(cfg: dict) -> dict:
    """Boards from config.yaml plus those found by `python run.py find-boards`."""
    import json
    merged = {k: list(v or []) for k, v in (cfg.get("ats") or {}).items()}
    f = ROOT / "data" / "ats_boards.json"
    if f.exists():
        try:
            found = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            found = {}
        for platform, entries in found.items():
            have = {(e if isinstance(e, str) else e.get("slug")) for e in merged.get(platform, [])}
            merged.setdefault(platform, []).extend(
                {"slug": e["slug"], "name": e["name"]} for e in entries if e["slug"] not in have)
    return merged


def cmd_find_boards(cfg: dict, _args) -> int:
    import json
    from jobbot.discover import discover
    names = cfg.get("ats_candidates") or []
    print(f"Probing {len(names)} companies on Greenhouse/Lever/Ashby/SmartRecruiters (about 1-3 min)...")
    found = discover(names)
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "ats_boards.json").write_text(json.dumps(found, indent=1), encoding="utf-8")
    total = 0
    for platform, entries in found.items():
        for e in entries:
            total += 1
            print(f"  {platform:16} {e['slug']:24} {e['india_jobs']} India job(s)  ({e['name']})")
    print(f"\n{total} board(s) with India jobs saved to data/ats_boards.json - used automatically by `run`.")
    return 0


def cmd_apply(cfg: dict, args) -> int:
    acfg = cfg.get("apply") or {}
    dry = not args.live and bool(acfg.get("dry_run", True))
    if args.live:
        dry = False
    print(do_apply(cfg, Store(ROOT / "data" / "jobs.db"), dry_run=dry, limit=args.limit,
                   headed=args.headed, retry_failed=args.retry_failed))
    return 0


def do_apply(cfg: dict, store: Store, sheets=None, *, dry_run: bool, limit=None, headed=False,
             retry_failed=False) -> str:
    from jobbot.apply_run import run_apply
    from jobbot.applier import Applier
    from jobbot.qa import Ctx
    from jobbot.scoring import years_experience
    acfg = cfg.get("apply") or {}
    tz = get_tz(cfg.get("timezone", "Asia/Kolkata"))
    now = datetime.now(timezone.utc).astimezone(tz)
    today = now.strftime("%Y-%m-%d")
    since = (now - timedelta(days=int(acfg.get("pending_days", 2)))).strftime("%Y-%m-%d")
    if sheets is None:
        try:
            sheets = make_sheets(cfg)
        except Exception as exc:  # noqa: BLE001
            logging.error("Google Sheets unavailable (%s); results are kept locally and sync next run.", exc)
    try:
        from jobbot.tailor import load_profile
        years = years_experience(load_profile(ROOT / cfg["resume"]["profile_file"]), now)
    except Exception:  # noqa: BLE001
        years = 2.5
    rules = [tuple(r) for r in acfg.get("qa_rules", []) if len(r) == 2]
    from jobbot.qa import load_bank
    bank_path = ROOT / "profile" / "my_answers.yaml"
    bank, known = load_bank(bank_path)
    ctx = Ctx(answers=cfg.get("answers", {}), years=years, rules=rules, bank=bank, bank_path=bank_path, known=known)
    if retry_failed:
        print(f"Re-queued {store.reset_failed(since)} previously failed job(s)")
    with Applier(headless=False if headed else bool(acfg.get("headless", True)),
                 shot_dir=ROOT / "output" / "apply_logs",
                 timeout=int(acfg.get("timeout_seconds", 120))) as ap:
        rep = run_apply(store=store, sheets=sheets, applier=ap, ctx=ctx, today=today, since_day=since,
                        dry_run=dry_run, limit=limit, max_per_day=int(acfg.get("max_per_day", 25)),
                        delay=tuple(acfg.get("delay_seconds", [15, 40])))
    out = rep.summary()
    if rep.pending == 0:
        out += ("\nWhy 0: only career-page jobs (apply type 'Auto') that are on the sheet, have a resume and no "
                "status yet are applied to.\nShortlisted since " + since + ": " + store.apply_overview(since))
    return out


def cmd_schedule(cfg: dict, args) -> int:
    """Create / show / remove the daily Windows Task Scheduler job (runs `python run.py run`)."""
    import subprocess
    name = "JobBot Daily"
    if os.name != "nt":
        print("Task Scheduler is Windows-only. On Linux/Mac add this cron line instead:\n"
              f"  {args.time.split(':')[1]} {args.time.split(':')[0]} * * *  cd {ROOT} && {sys.executable} run.py run")
        return 0

    def ps(cmd):
        return subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True)
    if args.remove:
        r = ps(f"Unregister-ScheduledTask -TaskName '{name}' -Confirm:$false")
        print("Removed." if r.returncode == 0 else r.stderr.strip() or "Nothing to remove.")
        return 0
    if args.status:
        r = ps(f"Get-ScheduledTask -TaskName '{name}' | Get-ScheduledTaskInfo | Format-List TaskName,LastRunTime,"
               "LastTaskResult,NextRunTime")
        print(r.stdout.strip() or "Not scheduled. Run: python run.py schedule")
        return 0
    py = sys.executable.replace("python.exe", "pythonw.exe") if args.hidden else sys.executable
    script = (f"$a = New-ScheduledTaskAction -Execute '{py}' -Argument '\"{ROOT / 'run.py'}\" run' "
              f"-WorkingDirectory '{ROOT}'; "
              f"$t = New-ScheduledTaskTrigger -Daily -At '{args.time}'; "
              "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries "
              "-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 3) "
              "-MultipleInstances IgnoreNew; "
              f"Register-ScheduledTask -TaskName '{name}' -Action $a -Trigger $t -Settings $s -Force | Out-Null")
    r = ps(script)
    if r.returncode != 0:
        print("Could not create the task automatically:\n" + (r.stderr or r.stdout).strip()
              + "\nTry running PowerShell as Administrator, or follow the manual steps in README.md.")
        return 1
    print(f"Scheduled '{name}' daily at {args.time}: python run.py run (collect -> shortlist -> sheet"
          f"{' -> apply' if (cfg.get('apply') or {}).get('auto_enabled') else ''}).\n"
          "It runs even if the PC was off at that time (as soon as it is on again). Check with: "
          "python run.py schedule --status")
    return 0


def cmd_fix_sheet(cfg: dict, _args) -> int:
    import re
    sheets = make_sheets(cfg)
    n = 0
    for ws in sheets.sh.worksheets():
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", ws.title):
            sheets.repair_daily_format(ws)
            n += 1
            print(f"Formatted tab {ws.title}")
    print(f"{n} daily tab(s) repaired: dropdowns, colours, widths." if n else "No dated tabs found.")
    return 0


def cmd_check_ats(cfg: dict, _args) -> int:
    lines = check_boards(load_ats_boards(cfg))
    print("\n".join(lines) if lines else "No boards configured under 'ats:' in config.yaml")
    return 0


def cmd_stats(_cfg: dict, _args) -> int:
    store = Store(ROOT / "data" / "jobs.db")
    for k, v in store.stats().items():
        print(f"{k}: {v}")
    return 0


def cmd_run(cfg: dict, args) -> int:
    lock = ROOT / "data" / "run.lock"
    (ROOT / "data").mkdir(exist_ok=True)
    if lock.exists() and (datetime.now().timestamp() - lock.stat().st_mtime) < 4 * 3600:
        print("Another run seems to be in progress (data/run.lock is less than 4 hours old). "
              "If that is wrong, delete the file and retry.")
        return 3
    lock.write_text(str(os.getpid()))
    try:
        return _cmd_run(cfg, args)
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _cmd_run(cfg: dict, args) -> int:
    s = cfg["search"]
    terms, results = list(s["terms"]), int(s["results_per_query"])
    if args.quick:
        terms, results = terms[:2], 10
    proxies = [p.strip() for p in os.environ.get("JOBSPY_PROXIES", "").split(",") if p.strip()]

    collectors = []
    if not args.only or args.only == "jobspy":
        collectors.append(JobSpyCollector(
            sites=cfg["sites"], terms=terms, locations=s["locations"],
            hours_old=int(s["hours_old"]), results_per_query=results,
            delay=tuple(s.get("delay_seconds", [6, 14])),
            max_retries=int(s.get("max_retries", 3)), proxies=proxies or None))
    if not args.only or args.only == "ats":
        collectors.append(AtsCollector(load_ats_boards(cfg)))

    sheets = None
    if not args.no_sheets:
        try:
            sheets = make_sheets(cfg)
        except Exception as exc:  # noqa: BLE001
            logging.error("Google Sheets unavailable (%s). Continuing locally; "
                          "rows will sync on the next successful run.", exc)
        if sheets is not None:
            try:
                sheets.ensure_layout(cfg.get("answers"))
            except Exception as exc:  # noqa: BLE001
                # Cosmetic/config tabs failed; the data tabs still work, so keep syncing.
                logging.warning("Sheet layout step failed (%s). Run `python run.py "
                                "init-sheet` to see details; continuing with the sheet.", exc)

    store = Store(ROOT / "data" / "jobs.db")
    report = run_pipeline(
        store=store, collectors=collectors, sheets=sheets,
        hours_old=int(s["hours_old"]), tz=get_tz(cfg.get("timezone", "Asia/Kolkata")),
        max_desc=int(cfg["sheet"].get("max_description_chars", 30000)),
        ats_backlog=bool(s.get("ats_include_backlog", True)),
        data_dir=ROOT / "data")
    print("\n" + report.summary())
    if not args.no_shortlist:
        try:
            print("\n--- Shortlist & resumes ---\n" + do_shortlist(cfg, store).summary())
        except Exception:  # noqa: BLE001 - collection already succeeded; never lose that
            logging.exception("Shortlist step failed; run `python run.py shortlist` to retry")
    if not args.no_shortlist and sheets is not None:
        try:
            print("\n--- Daily sheet tab ---\n" + do_daily_tab(cfg, store, sheets=sheets))
        except Exception:  # noqa: BLE001
            logging.exception("Daily tab step failed; run `python run.py daily-tab` to retry")
    if not args.no_shortlist and not args.no_apply and (cfg.get("apply") or {}).get("auto_enabled"):
        try:
            print("\n--- Auto-apply ---\n" + do_apply(
                cfg, store, sheets, dry_run=bool((cfg.get("apply") or {}).get("dry_run", True))))
        except Exception:  # noqa: BLE001
            logging.exception("Auto-apply step failed; run `python run.py apply` to retry")
    return 0 if report.found else 2  # exit 2 = nothing collected (blocked / offline)


def do_daily_tab(cfg: dict, store: Store, sheets=None, people: bool = True) -> str:
    """Phase 3: write today's shortlist to a dated sheet tab, then fill recruiter contacts."""
    from jobbot.daily import run_people, sync_daily_tab
    day = datetime.now(timezone.utc).astimezone(
        get_tz(cfg.get("timezone", "Asia/Kolkata"))).strftime("%Y-%m-%d")
    sheets = sheets or make_sheets(cfg)
    auto = bool((cfg.get("apply") or {}).get("auto_enabled", False))
    lines = [f"Tab '{day}': {sync_daily_tab(store, sheets, day, auto)} new row(s) written"]
    pc = cfg.get("people") or {}
    profile_dir = ROOT / "data" / "linkedin_profile"
    if not people or not pc.get("enabled", True):
        return "\n".join(lines)
    if not profile_dir.exists():
        lines.append("Recruiter contacts skipped - run `python run.py linkedin-login` once to enable")
        return "\n".join(lines)
    from jobbot.people import LinkedInFinder
    with LinkedInFinder(profile_dir, headless=bool(pc.get("headless", True)),
                        delay=tuple(pc.get("delay_seconds", [10, 25]))) as finder:
        if not finder.logged_in():
            lines.append("LinkedIn session expired - run `python run.py linkedin-login` again")
        else:
            st = run_people(store, sheets, finder, day, max_per_day=int(pc.get("max_per_day", 20)))
            lines.append(f"Contacts: searched {st['searched']}, found {st['found']}, "
                         f"written to sheet {st['pushed']}" + (" (LinkedIn asked for "
                         "verification - stopped early)" if st["blocked"] else ""))
    return "\n".join(lines)


def cmd_daily_tab(cfg: dict, args) -> int:
    print(do_daily_tab(cfg, Store(ROOT / "data" / "jobs.db"), people=not args.no_people))
    return 0


def cmd_linkedin_login(_cfg: dict, _args) -> int:
    from jobbot.people import linkedin_login
    print("A browser window will open. Sign in to LinkedIn there (solve any verification); "
          "it closes by itself once you reach your feed. You have 5 minutes.")
    ok = linkedin_login(ROOT / "data" / "linkedin_profile")
    print("LinkedIn session saved." if ok else "Sign-in not completed. Run this command again.")
    return 0 if ok else 1


def main() -> int:
    load_env(ROOT / ".env")
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-sheet")
    sub.add_parser("check-ats")
    sub.add_parser("stats")
    r = sub.add_parser("run")
    r.add_argument("--quick", action="store_true")
    r.add_argument("--no-sheets", action="store_true")
    r.add_argument("--only", choices=["jobspy", "ats"])
    r.add_argument("--no-apply", action="store_true", help="skip the auto-apply step")
    r.add_argument("--no-shortlist", action="store_true",
                   help="collect only; skip the shortlist + resume step")
    s = sub.add_parser("shortlist", help="shortlist best jobs + build tailored resume PDFs")
    s.add_argument("--no-resume", action="store_true", help="select only, no PDFs")
    s.add_argument("--no-llm", action="store_true", help="skip AI; reorder original wording")
    s.add_argument("--limit", type=int, help="shortlist at most N new jobs (for testing)")
    s.add_argument("--regen", action="store_true",
                   help="rebuild today's resume PDFs (e.g. after adding AI keys)")
    sub.add_parser("sample-resume", help="render a sample PDF to check the layout")
    d = sub.add_parser("daily-tab", help="write today's shortlist to a dated sheet tab + contacts")
    d.add_argument("--no-people", action="store_true", help="skip the LinkedIn recruiter lookup")
    sc = sub.add_parser("schedule", help="run the whole daily job automatically (Windows Task Scheduler)")
    sc.add_argument("--time", default="06:00", help="24h time, default 06:00")
    sc.add_argument("--remove", action="store_true")
    sc.add_argument("--status", action="store_true")
    sc.add_argument("--hidden", action="store_true", help="run without a console window")
    sub.add_parser("fix-sheet", help="re-apply dropdowns/colours/widths on all dated tabs")
    sub.add_parser("find-boards", help="discover company career boards (Greenhouse/Lever/Ashby)")
    ap = sub.add_parser("apply", help="auto-apply to pending career-page jobs (dry run unless --live)")
    ap.add_argument("--live", action="store_true", help="really submit applications")
    ap.add_argument("--limit", type=int, help="process at most N jobs")
    ap.add_argument("--retry-failed", action="store_true",
                    help="retry jobs that failed earlier (after adding answers to profile/my_answers.yaml)")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    sub.add_parser("linkedin-login", help="one-time LinkedIn sign-in for recruiter lookup")
    args = p.parse_args()
    setup_logging()
    cfg = load_config()
    return {"init-sheet": cmd_init_sheet, "check-ats": cmd_check_ats, "stats": cmd_stats,
            "run": cmd_run, "shortlist": cmd_shortlist,
            "sample-resume": cmd_sample_resume, "daily-tab": cmd_daily_tab,
            "linkedin-login": cmd_linkedin_login, "find-boards": cmd_find_boards,
            "apply": cmd_apply, "fix-sheet": cmd_fix_sheet,
            "schedule": cmd_schedule}[args.cmd](cfg, args)


if __name__ == "__main__":
    sys.exit(main())
