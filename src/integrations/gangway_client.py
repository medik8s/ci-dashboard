"""
Gangway API client for on-demand Prow job triggering.

Uses the OpenShift CI Gangway REST API to trigger periodic jobs
and poll execution status.
"""

import os
import logging
import urllib.request
import urllib.error
import json

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


_gangway_client = GangwayClient()


def get_gangway_client():
    return _gangway_client
