"""
Gangway API client for on-demand Prow job triggering.

Uses the OpenShift CI Gangway REST API to trigger periodic jobs
and poll execution status.
"""

import os
import re
import logging
import urllib.request
from urllib.parse import quote
import urllib.error
import json
from datetime import datetime

logger = logging.getLogger(__name__)

GANGWAY_BASE_URL = "https://gangway-ci.apps.ci.l2s4.p1.openshiftapps.com/v1"
PROW_BASE_URL = "https://prow.ci.openshift.org"
PROW_GCS_BUCKET = "test-platform-results"

_OPERATOR_PATTERN = re.compile(r'-e2e-([a-z0-9]+)-')

_job_map_cache = None


def _load_job_patterns_from_config():
    config_path = os.path.join(os.path.dirname(__file__), '..', '..', 'config.yaml')
    try:
        import yaml
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        return cfg.get('collector', {}).get('prow_gcs', {}).get('job_patterns', [])
    except Exception:
        logger.warning("Could not load config.yaml, falling back to empty job list")
        return None


def get_operator_job_map():
    """Build operator-to-jobs mapping from config.yaml job_patterns.

    Returns dict mapping operator name to list of periodic job names.
    Derives operator from the '-e2e-<operator>-' segment in each job name.
    """
    global _job_map_cache
    if _job_map_cache is not None:
        return _job_map_cache

    loaded = _load_job_patterns_from_config()
    if loaded is None:
        return {}

    result = {}
    skipped = []
    for job in loaded:
        m = _OPERATOR_PATTERN.search(job)
        if m:
            result.setdefault(m.group(1), []).append(job)
        else:
            skipped.append(job)
    if skipped:
        logger.warning("Jobs not triggerable (no operator match): %s", skipped)
    _job_map_cache = result
    return result


def get_all_triggerable_jobs():
    """Return flat list of all periodic job names from config."""
    job_map = get_operator_job_map()
    return [job for jobs in job_map.values() for job in jobs]


def operator_from_job_name(job_name):
    """Extract operator name from a job name string, or return None."""
    m = _OPERATOR_PATTERN.search(job_name)
    return m.group(1) if m else None


def resolve_trigger_target(job_name_or_operator):
    """Resolve an operator shorthand or full job name to a canonical job name.

    Returns (job_name, operator, error). On success error is None.
    On failure job_name and operator are None.
    """
    job_map = get_operator_job_map()
    all_jobs = get_all_triggerable_jobs()
    key = job_name_or_operator.strip().lower()

    if key in all_jobs or job_name_or_operator in all_jobs:
        job_name = job_name_or_operator if job_name_or_operator in all_jobs else key
        op = operator_from_job_name(job_name) or key
        return job_name, op, None
    elif key in job_map:
        jobs = job_map[key]
        if len(jobs) == 1:
            return jobs[0], key, None
        return None, None, (
            f"Operator '{key}' has {len(jobs)} jobs. "
            f"Specify the full job name: {', '.join(jobs)}"
        )
    else:
        return None, None, (
            f"Unknown job or operator: {key}. "
            f"Valid operators: {', '.join(sorted(job_map.keys()))}"
        )


def _build_spyglass_url(job_name, build_id):
    """Build a Prow Spyglass URL from a job name and build ID."""
    return (f"{PROW_BASE_URL}/view/gs/{PROW_GCS_BUCKET}"
            f"/logs/{quote(job_name, safe='')}/{build_id}")


def _find_best_prow_build(job_name, triggered_at_str):
    """Fetch Prow job history and return the build dict closest to triggered_at_str."""
    if not job_name or not triggered_at_str:
        return None
    history_url = (f"{PROW_BASE_URL}/job-history/gs/{PROW_GCS_BUCKET}"
                   f"/logs/{quote(job_name, safe='')}")
    try:
        req = urllib.request.Request(history_url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            html = resp.read().decode()
        marker_idx = html.find('var allBuilds')
        if marker_idx == -1:
            logger.warning("_find_best_prow_build: allBuilds not found for %s", job_name)
            return None
        arr_start = html.find('[', marker_idx)
        if arr_start == -1:
            return None
        try:
            builds, _ = json.JSONDecoder().raw_decode(html[arr_start:])
        except json.JSONDecodeError:
            logger.warning("_find_best_prow_build: invalid allBuilds JSON for %s", job_name)
            return None
        if not builds:
            return None
        ts = triggered_at_str.replace(' ', 'T')
        if not re.search(r'(?:Z|[+-]\d{2}:\d{2})$', ts):
            ts += '+00:00'
        trigger_ts = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        best = None
        best_delta = None
        for b in builds:
            started = b.get('Started', '')
            if not started:
                continue
            try:
                build_ts = datetime.fromisoformat(started.replace('Z', '+00:00'))
            except ValueError:
                continue
            delta = (build_ts - trigger_ts).total_seconds()
            if delta < -30 or delta > 600:
                continue
            candidate_key = (delta < 0, abs(delta))
            best_key = (best_delta < 0, abs(best_delta)) if best_delta is not None else None
            if best_key is None or candidate_key < best_key:
                best_delta = delta
                best = b
        if best:
            logger.debug("_find_best_prow_build matched %s delta=%.0fs", job_name, best_delta)
        return best
    except Exception as e:
        logger.warning("_find_best_prow_build failed for %s: %s", job_name, e)
    return None


class GangwayClient:
    def __init__(self):
        self.token = os.environ.get("PROW_GANGWAY_TOKEN", "")
        self.enabled = bool(self.token)
        if not self.enabled:
            logger.warning("PROW_GANGWAY_TOKEN not set, Gangway trigger disabled")

    def _request(self, method, path, body=None):
        url = f"{GANGWAY_BASE_URL}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if not raw:
                    return {}, resp.status
                try:
                    return json.loads(raw), resp.status
                except json.JSONDecodeError:
                    logger.error("Gangway %s %s returned non-JSON (status %d): %s",
                                 method, path, resp.status, raw[:200])
                    return {"error": "Non-JSON response from Gangway"}, resp.status
        except urllib.error.HTTPError as e:
            error_body = e.read().decode(errors="replace")
            logger.error("Gangway %s %s returned %d: %s", method, path, e.code, error_body[:500])
            return {"error": f"Gangway returned HTTP {e.code}"}, e.code
        except Exception as e:
            logger.error("Gangway %s %s failed: %s", method, path, e)
            return {"error": "Gangway request failed"}, 0

    def trigger_job(self, job_name, env_overrides=None):
        """Trigger a periodic job by its canonical (pre-resolved) job name."""
        payload = {"job_name": job_name, "job_execution_type": "1"}
        if env_overrides:
            payload["pod_spec_options"] = {"envs": env_overrides}
        resp, status = self._request("POST", "/executions", payload)
        if 200 <= status < 300:
            execution_id = resp.get("id")
            if not execution_id:
                return None, f"Gangway returned success but no execution id: {resp}"
            return {
                "execution_id": execution_id,
                "job_name": job_name,
                "operator": operator_from_job_name(job_name) or "unknown",
                "status": resp.get("job_status", "TRIGGERED"),
            }, None
        return None, resp.get("error", f"HTTP {status}")

    def get_execution_status(self, execution_id):
        resp, status = self._request("GET", f"/executions/{execution_id}")
        if 200 <= status < 300:
            return resp, None
        return None, resp.get("error", f"HTTP {status}")

    @staticmethod
    def resolve_prow_url(job_name, triggered_at_str):
        """Find the Prow Spyglass URL for a triggered job by matching timestamps."""
        build = _find_best_prow_build(job_name, triggered_at_str)
        if not build:
            return None
        build_id = build.get('ID', '')
        if not build_id:
            return None
        return _build_spyglass_url(job_name, build_id)

    @staticmethod
    def resolve_prow_result(job_name, triggered_at_str):
        """Find the Prow build result and URL for a triggered job.

        Returns (result, url) where result is the raw Prow Result field
        value (typically SUCCESS, FAILURE, or ABORTED) or None.
        """
        build = _find_best_prow_build(job_name, triggered_at_str)
        if not build:
            return None, None
        build_id = build.get('ID', '')
        result = build.get('Result', '')
        if not build_id or not result:
            return None, None
        return result, _build_spyglass_url(job_name, build_id)


_gangway_client = GangwayClient()


def get_gangway_client():
    return _gangway_client
