"""Run source-backed seat-map canaries on a private/self-hosted runner.

The configuration stays outside the repository because a useful target is a
currently bookable showtime scope, not a permanent fixture. Example:

    {
      "days": 7,
      "targets": [
        {"name": "amc", "venue_id": "amc-metreon-16", "expect": "map"},
        {"name": "regal-fandango", "venue_id": "regal-example", "expect": "map",
         "allowed_capture_sources": ["regal", "fandango"]},
        {"name": "vista", "venue_id": "metrograph", "expect": "map"}
      ]
    }

The runner never selects a seat, creates a hold, or enters checkout.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import tempfile
import traceback

from screenwatch.browser import BrowserTransport
from screenwatch.service.defaults import default_service
from screenwatch.service.harvest import HarvestService


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run live auditorium-map canaries")
    config = parser.add_mutually_exclusive_group(required=True)
    config.add_argument("--config", type=pathlib.Path)
    config.add_argument("--config-json")
    parser.add_argument("--artifacts", type=pathlib.Path, default=pathlib.Path("canary-artifacts"))
    return parser.parse_args()


def _load_config(args: argparse.Namespace) -> dict:
    raw = args.config.read_text(encoding="utf-8") if args.config else args.config_json
    payload = json.loads(raw)
    targets = payload.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("canary config needs a non-empty targets list")
    for target in targets:
        if not target.get("name") or not target.get("venue_id"):
            raise ValueError("every canary needs name and venue_id")
        if target.get("expect", "map") not in {"map", "typed_failure"}:
            raise ValueError("expect must be map or typed_failure")
    return payload


def _failure_rows(store, run_id: str) -> list[dict]:
    rows = store._conn.execute(
        """SELECT provider, stage, code, retryable, action, message, source_url,
                  status_code, raw_capture_id, context, captured_at
           FROM harvest_failures WHERE run_id=? ORDER BY failure_id""",
        (run_id,),
    ).fetchall()
    return [
        {
            **dict(row),
            "context": json.loads(row["context"] or "{}"),
        }
        for row in rows
    ]


def _target_ok(target: dict, result: dict, failures: list[dict], store) -> tuple[bool, str]:
    expected = target.get("expect", "map")
    if expected == "typed_failure":
        wanted = set(target.get("failure_codes") or [])
        observed = {row["code"] for row in failures}
        if not failures:
            return False, "expected a typed collector failure, but none was persisted"
        if wanted and not (wanted & observed):
            return False, f"expected one of {sorted(wanted)}, observed {sorted(observed)}"
        return True, f"typed failure persisted: {', '.join(sorted(observed))}"

    minimum = int(target.get("min_maps", 1))
    if int(result.get("maps_captured", 0)) < minimum:
        return False, f"captured {result.get('maps_captured', 0)} maps; expected {minimum}"
    allowed = set(target.get("allowed_capture_sources") or [])
    if allowed:
        observed = {
            row[0]
            for row in store._conn.execute(
                """SELECT DISTINCT rc.source
                   FROM raw_captures rc
                   JOIN seat_probes sp ON sp.probe_id=rc.probe_id
                   WHERE sp.venue_id=?""",
                (target["venue_id"],),
            ).fetchall()
        }
        if not (allowed & observed):
            return False, f"capture sources {sorted(observed)} exclude {sorted(allowed)}"
    return True, f"captured {result['maps_captured']} map(s)"


def main() -> int:
    args = _arguments()
    config = _load_config(args)
    args.artifacts.mkdir(parents=True, exist_ok=True)
    db_path = args.artifacts / "canaries.db"
    har_path = args.artifacts / "browser.har"
    reports: list[dict] = []
    failures_seen = False

    with tempfile.TemporaryDirectory(prefix="screenwatch-canary-profile-") as profile:
        browser = BrowserTransport(profile_dir=profile, record_har_path=har_path)
        search, _watches, store = default_service(str(db_path), browser=browser)
        try:
            collector = HarvestService(search)
            for target in config["targets"]:
                name = str(target["name"])
                try:
                    result = collector.harvest(
                        venue_id=str(target["venue_id"]),
                        days=int(target.get("days", config.get("days", 7))),
                        max_maps=int(target.get("max_maps", 1)),
                        max_attempts=int(target.get("max_attempts", 3)),
                    ).to_dict()
                    failure_rows = _failure_rows(store, str(result["run_id"]))
                    ok, detail = _target_ok(target, result, failure_rows, store)
                    report = {
                        "target": target,
                        "ok": ok,
                        "detail": detail,
                        "result": result,
                        "failures": failure_rows,
                    }
                except Exception as exc:  # noqa: BLE001 - canary must preserve evidence
                    ok = False
                    report = {
                        "target": target,
                        "ok": False,
                        "detail": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc(),
                    }
                if not ok:
                    failures_seen = True
                    diagnostic_url = next(
                        (
                            row.get("source_url")
                            for row in report.get("failures", [])
                            if row.get("source_url")
                        ),
                        None,
                    )
                    if not diagnostic_url:
                        venue = search.directory.get(str(target["venue_id"]))
                        diagnostic_url = venue.url if venue else None
                    if diagnostic_url:
                        try:
                            browser.visit(str(diagnostic_url), wait_for_challenge=True)
                        except Exception as exc:  # noqa: BLE001 - preserve the first failure
                            report["diagnostic_visit_error"] = (
                                f"{type(exc).__name__}: {exc}"
                            )
                    if browser.started:
                        try:
                            report["browser_diagnostics"] = browser.save_diagnostics(
                                args.artifacts, label=name
                            )
                        except Exception as exc:  # noqa: BLE001 - never mask canary result
                            report["browser_diagnostics_error"] = (
                                f"{type(exc).__name__}: {exc}"
                            )
                (args.artifacts / f"{name}.json").write_text(
                    json.dumps(report, indent=2, sort_keys=True), encoding="utf-8"
                )
                reports.append(report)
        finally:
            browser.close()
            store.close()

    summary = {"ok": not failures_seen, "reports": reports}
    (args.artifacts / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if failures_seen else 0


if __name__ == "__main__":
    raise SystemExit(main())
