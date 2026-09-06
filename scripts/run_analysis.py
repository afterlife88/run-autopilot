"""
LLM run analysis: classify the workout and describe the effort.

Calls the Claude CLI (Sonnet) headlessly with a compact summary of the run.
Optional by design — if the CLI is missing, times out, or returns garbage,
the caller falls back to the heuristic classification and no analysis line.
"""
import json
import logging
import re
import shutil
import subprocess

log = logging.getLogger("auto_sync")

CLAUDE_BIN = "claude"
MODEL = "sonnet"
TIMEOUT_SEC = 120

VALID_TYPES = {"Recovery", "Easy", "Moderate", "Tempo", "Threshold",
               "Interval", "Long Run", "Race"}

PROMPT = """You are a running coach analyzing one run. Output ONLY a JSON object, nothing else:
{{"type": "<one of: Recovery, Easy, Moderate, Tempo, Threshold, Interval, Long Run, Race>",
 "name": "<short workout name, e.g. '10×1k', 'Long Run 30k', 'Easy 10k'>",
 "analysis": "<2-3 sentences in English on effort and execution, with concrete numbers>"}}

Run data:
{data}

Heuristic classifier suggests: type={htype}, name={hname}

Athlete context: CP {cp}W. His typical long runs are 24-30k at 88-95% CP (routine, not heroic);
easy runs 70-80% CP; threshold reps ~105-110% CP. Judge effort against HIS baselines.
He is T1D — if glucose data is present and notable, one short remark is fine.
Rules: power-based judgment (pace lies in heat/wind); no generic advice; no exclamation marks;
mention negative/positive split, drift, or evenness only if the lap data supports it."""


def _summarize(workout, preview, pct_cp):
    laps = preview.get("intervals", [])
    lap_rows = [
        f"{iv.get('time')} {iv.get('pace')}/km {iv.get('power')}W {iv.get('hr')}bpm"
        for iv in laps[:40]
    ]
    weather = workout.get("weather") or {}
    stryd = workout.get("stryd") or {}
    glucose = [iv.get("glucose", {}).get("avg") for iv in laps
               if iv.get("glucose", {}).get("avg") is not None]
    return {
        "date": workout.get("date"),
        "distance_km": preview.get("total_distance_km"),
        "duration_min": preview.get("total_duration_min"),
        "avg_hr": preview.get("avg_hr"),
        "pct_cp": pct_cp,
        "rss": stryd.get("rss"),
        "weather": {k: weather.get(k) for k in
                    ("temperature_c", "wind_speed_kmh", "wind_gust_kmh")
                    if weather.get(k) is not None},
        "glucose_mmol": {"start": glucose[0], "end": glucose[-1]} if glucose else None,
        "laps": lap_rows,
    }


def analyze_run(workout, preview, pct_cp, htype, hname, cp):
    """Returns {"type", "name", "analysis"} or None on any failure."""
    if not shutil.which(CLAUDE_BIN):
        return None

    prompt = PROMPT.format(
        data=json.dumps(_summarize(workout, preview, pct_cp), ensure_ascii=False),
        htype=htype, hname=hname, cp=round(cp) if cp else "unknown",
    )
    try:
        proc = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--model", MODEL],
            capture_output=True, text=True, timeout=TIMEOUT_SEC,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("  llm analysis failed: %s", e)
        return None
    if proc.returncode != 0:
        log.warning("  llm analysis rc=%d: %s", proc.returncode, proc.stderr[:120])
        return None

    m = re.search(r"\{.*\}", proc.stdout, re.DOTALL)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None

    if out.get("type") not in VALID_TYPES or not out.get("name"):
        return None
    out["name"] = str(out["name"])[:60]
    out["analysis"] = str(out.get("analysis") or "").strip()[:600]
    return out
