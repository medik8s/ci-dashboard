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

OPERATOR_JOB_MAP = {
    "far": "periodic-ci-medik8s-system-tests-main-4.22-konflux-e2e-far-weekly-aws",
    "sbr": "periodic-ci-medik8s-system-tests-main-4.22-konflux-e2e-sbr-weekly-aws-odf",
    "snr": "periodic-ci-medik8s-system-tests-main-4.22-konflux-e2e-snr-weekly-aws",
    "mdr": "periodic-ci-medik8s-system-tests-main-4.22-konflux-e2e-mdr-weekly-aws",
    "nmo": "periodic-ci-medik8s-system-tests-main-4.22-konflux-e2e-nmo-weekly-aws",
    "nhc": "periodic-ci-medik8s-system-tests-main-4.22-konflux-e2e-nhc-weekly-aws",
}


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

    def trigger_job(self, operator):
        job_name = OPERATOR_JOB_MAP.get(operator.lower())
        if not job_name:
            return None, f"Unknown operator: {operator}. Valid: {', '.join(sorted(OPERATOR_JOB_MAP))}"
        payload = {"job_name": job_name, "job_execution_type": "1"}
        resp, status = self._request("POST", "/executions", payload)
        if 200 <= status < 300:
            execution_id = resp.get("id")
            if not execution_id:
                return None, f"Gangway returned success but no execution id: {resp}"
            return {
                "execution_id": execution_id,
                "job_name": job_name,
                "operator": operator.lower(),
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
        prow_base = "https://prow.ci.openshift.org"
        bucket = "test-platform-results"
        history_url = f"{prow_base}/job-history/gs/{bucket}/logs/{quote(job_name, safe='')}"
        try:
            req = urllib.request.Request(history_url)
            with urllib.request.urlopen(req, timeout=5) as resp:
                html = resp.read().decode()
            marker_idx = html.find('var allBuilds')
            if marker_idx == -1:
                logger.warning("resolve_prow_url: allBuilds not found for %s", job_name)
                return None
            arr_start = html.find('[', marker_idx)
            if arr_start == -1:
                return None
            try:
                builds, _ = json.JSONDecoder().raw_decode(html[arr_start:])
            except json.JSONDecodeError:
                logger.warning("resolve_prow_url: invalid allBuilds JSON for %s", job_name)
                return None
            if not builds:
                return None
            if not triggered_at_str:
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
                build_id = best.get('ID', '')
                if build_id:
                    logger.debug("resolve_prow_url matched %s delta=%.0fs", job_name, best_delta)
                    return f"{prow_base}/view/gs/{bucket}/logs/{quote(job_name, safe='')}/{build_id}"
        except Exception as e:
            logger.warning("resolve_prow_url failed for %s: %s", job_name, e)
        return None


_gangway_client = GangwayClient()


def get_gangway_client():
    return _gangway_client
