"""
Direct GCS Collector - Access Prow test data directly from GCS

Based on prow-mcp-server approach:
1. Fetch job metadata from Prow API (prowjobs.js)
2. Fetch test artifacts from GCS storage
3. Parse JUnit XML for test results
4. Fetch build logs for failed tests

No MCP, no ReportPortal - direct access to Prow data.
"""

import os
import re
import subprocess
import xml.etree.ElementTree as ET
import logging
from datetime import datetime, timezone
from typing import Callable, List, Dict, Any, Optional, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .base import BaseCollector, TestResult, JobRun, TestStatus

logger = logging.getLogger(__name__)

PROW_PR_PATH_RE = re.compile(
    r"pr-logs/pull/(?P<repo>[^/]+)/(?P<pr_number>\d+)/"
    r"(?P<job_name>[^/]+)/(?P<build_id>\d+)"
)

_GINKGO_IT_RE = re.compile(r'^\[It\]\s*')
_GINKGO_DECORATOR_RE = re.compile(
    r'\s*\[(Serial|Slow|Disruptive|Flaky|sig-[\w-]+)\]',
    flags=re.IGNORECASE,
)


class ProwGCSCollector(BaseCollector):
    """Collector that accesses Prow data directly via GCS"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)

        # Prow instance URLs (must be configured)
        self.prow_url = config.get('prow_url')
        self.gcs_url = config.get('gcs_url')

        if not self.prow_url or not self.gcs_url:
            raise ValueError("prow_gcs collector requires 'prow_url' and 'gcs_url' in config")

        # Authentication (optional for public Prow instances)
        self.api_token = self._get_api_token(config)

        # GCS bucket name
        self.bucket = config.get('bucket', 'test-platform-results')

        # Job patterns
        self.job_names = config.get('job_names', [])
        self.max_workers = config.get('max_workers', 5)

        # HTTP session
        self.session = requests.Session()
        headers = {'Accept': 'application/json'}
        if self.api_token:
            headers['Authorization'] = f'Bearer {self.api_token}'
        self.session.headers.update(headers)

    def _get_api_token(self, config: Dict[str, Any]) -> Optional[str]:
        """Get API token from config, environment, or oc CLI (optional for public Prow)"""
        # Try config first
        token = config.get('api_token')
        if token:
            return token

        # Try environment variable
        token = os.environ.get('API_KEY')
        if token:
            return token

        # Try oc CLI
        try:
            token = subprocess.check_output(['oc', 'whoami', '-t'], stderr=subprocess.DEVNULL).decode().strip()
            if token:
                return token
        except Exception:
            pass

        # No token found - this is OK for public Prow instances
        return None

    @property
    def name(self) -> str:
        return "prow-gcs"

    def health_check(self) -> bool:
        """Check if Prow API is accessible"""
        try:
            url = f"{self.prow_url}/"
            logger.info(f"[prow_gcs] Health check URL: {url}")
            response = self.session.head(url, timeout=10)
            logger.info(f"[prow_gcs] Health check response: status={response.status_code}")

            if response.status_code == 403:
                logger.error(f"[prow_gcs] Authentication failed (HTTP 403)")
                logger.error(f"[prow_gcs] The Prow API token is missing, invalid, or expired")
                logger.error(f"[prow_gcs] To renew the token:")
                logger.error(f"[prow_gcs]   1. Login to OpenShift cluster: oc login https://api.ci.l2s4.p1.openshiftapps.com")
                logger.error(f"[prow_gcs]   2. Get new token: oc whoami -t")
                logger.error(f"[prow_gcs]   3. Update secret: oc create secret generic prow-api-token --from-literal=token=YOUR_TOKEN --dry-run=client -o yaml | oc apply -f -")
                logger.error(f"[prow_gcs]   4. Restart deployment: oc rollout restart deployment/ci-dashboard")
                return False
            elif response.status_code != 200:
                logger.error(f"[prow_gcs] Health check failed: HTTP {response.status_code}")
                logger.error(f"[prow_gcs] Response: {response.text[:500]}")
                return False

            return True
        except Exception as e:
            logger.error(f"[prow_gcs] Health check failed with exception: {e}")
            import traceback
            logger.error(f"[prow_gcs] Traceback: {traceback.format_exc()}")
            return False

    def _extract_version_platform(self, job_name: str) -> tuple[str, str]:
        """Extract version and platform from job name"""
        version = 'unknown'
        platform = 'unknown'

        # Extract version (e.g., 4.21, 4.22, 5.0) from release- or main- prefixed segments
        version_match = re.search(r'(?:release|main)-(\d+\.\d+)', job_name)
        if version_match:
            version = version_match.group(1)

        # Extract platform
        platforms = ['aws', 'gcp', 'azure', 'vsphere', 'nutanix', 'metal']
        for p in platforms:
            if p in job_name.lower():
                platform = p
                break

        return version, platform

    def _extract_test_name(self, raw_name: str) -> tuple[str, str, Optional[str]]:
        """
        Extract clean test name, description, and Polarion ID from raw name.

        Handles both OCP-XXXXX format (upstream) and Ginkgo format (medik8s):
        - "OCP-25593:sgao:...[sig-windows] Prevent scheduling..."
        - "[It] FAR Post Deployment tests Verify ... [far, 66026, test_id:66026]"

        Returns: (test_id, description, polarion_id)
        """
        polarion_match = re.search(r'test_id:\s*(\d+)', raw_name, re.IGNORECASE)
        polarion_id = f"OCP-{polarion_match.group(1)}" if polarion_match else None

        ocp_match = re.search(r'OCP-\d+', raw_name)

        if ocp_match:
            test_id = ocp_match.group(0)
            if not polarion_id:
                polarion_id = test_id
            elif polarion_id != test_id:
                logger.warning(f"Polarion ID mismatch: polarion_id={polarion_id} vs OCP token {test_id} in: {raw_name}")

            bracket_match = re.search(
                r'\[([^\]]*' + re.escape(test_id) + r'[^\]]*)\]', raw_name
            )
            if bracket_match:
                before = raw_name[:bracket_match.start()].strip()
                before = _GINKGO_IT_RE.sub('', before)
                before = _GINKGO_DECORATOR_RE.sub('', before)
                before = re.sub(r'\s*\[[^\]]+\]\s*$', '', before).strip()
                if before:
                    return test_id, before, polarion_id

            after_id = raw_name.split(test_id, 1)[-1]
            description = after_id.strip(':- \t')
            description = re.sub(r'\s*\[[^\]]+\]', '', description)
            description = re.sub(r'^[:\-\s]+', '', description)
            return test_id, description.strip() if description else test_id, polarion_id

        description = raw_name.strip()
        description = _GINKGO_IT_RE.sub('', description)
        description = _GINKGO_DECORATOR_RE.sub('', description)
        description = re.sub(
            r'\s*\[[^\]]*test_id:\s*\d+[^\]]*\]\s*$',
            '',
            description,
            flags=re.IGNORECASE,
        )
        description = description.strip()
        test_id = description if description else raw_name
        return test_id, description, polarion_id

    def _parse_ocp_version(self, text: str) -> Optional[str]:
        """Parse full OCP version from ipi-install-install log."""
        match = re.search(r'(\d+\.\d+\.\d+-0\.nightly-\d{4}-\d{2}-\d{2}-\d{6})', text)
        if match:
            return match.group(1)
        match = re.search(r'(\d+\.\d+\.\d+-(?:rc|ec)\.\d+)', text)
        if match:
            return match.group(1)
        match = re.search(r'(\d+\.\d+\.\d+)', text)
        return match.group(1) if match else None

    def _parse_csv_version(self, text: str) -> Optional[str]:
        """Parse operator CSV version from medik8s-operator-subscribe log."""
        match = re.search(r'Found CSV:\s*(\S+)', text)
        return match.group(1) if match else None

    def _parse_fbc_image(self, text: str) -> Optional[str]:
        """Parse FBC catalog image from medik8s-catalogsource log."""
        match = re.search(r'with image:\s*(\S+)', text)
        return match.group(1) if match else None

    def _parse_failure_reason(self, text: str) -> Optional[str]:
        """Parse the Prow failure reason from build-log.txt (last match wins)."""
        matches = re.findall(
            r"Reporting job state 'failed' with reason '([^']+)'", text
        )
        return matches[-1] if matches else None

    _PLUMBING_TOKENS = frozenset({
        'executing_graph', 'step_failed', 'executing_test',
        'utilizing_lease', 'utilizing_ip_pool',
    })

    def _extract_failed_step(self, raw_reason: str) -> str:
        """Extract a human-readable step name from the colon-delimited reason.

        Returns 'parent / leaf' when two or more meaningful tokens exist,
        so users see context like 'importing_release / pod_pending' instead
        of just 'pod_pending'.
        """
        parts = [p.strip() for p in raw_reason.split(':') if p.strip()]
        meaningful = [p for p in parts if p not in self._PLUMBING_TOKENS]
        if len(meaningful) >= 2:
            return f"{meaningful[-2]} / {meaningful[-1]}"
        if meaningful:
            return meaningful[-1]
        return parts[-1] if parts else None

    def _classify_failure(self, raw_reason: str) -> str:
        """Classify a Prow failure reason into a category (strict priority order)."""
        if not raw_reason:
            return 'unknown'
        tokens = {t.strip() for t in raw_reason.lower().split(':')} - self._PLUMBING_TOKENS
        if tokens & {'pod_pending', 'importing_release', 'scheduling',
                     'cloning_source'}:
            return 'infra'
        if tokens & {'ipi-install', 'ipi_install', 'bootstrap'}:
            return 'install'
        if tokens & {'catalogsource', 'subscribe', 'odf', 'set-odf'}:
            return 'setup'
        if tokens & {'e2e-test', 'e2e_test', 'executing_multi_stage_test'}:
            return 'test'
        return 'unknown'

    def _try_parse_from_steps(self, base: str, steps: Sequence[str], parse_func: Callable[[str], Optional[str]]) -> Optional[str]:
        """Try multiple step names, return first truthy parsed result."""
        for step in steps:
            log = self._fetch_gcs_text(f"{base}/{step}/build-log.txt")
            if log:
                parsed = parse_func(log)
                if parsed:
                    return parsed
        return None

    def _fetch_gcs_text(self, url: str) -> Optional[str]:
        """Fetch text content from a GCS URL, returning None on failure."""
        try:
            response = self.session.get(url, timeout=15)
            if response.status_code == 200:
                return response.text
        except Exception as e:
            logger.debug(f"[prow_gcs] Could not fetch {url}: {e}")
        return None

    def _fetch_gcs_tail(self, url: str, tail_bytes: int = 51200) -> Optional[str]:
        """Fetch only the last N bytes of a GCS object (for failure reason parsing)."""
        try:
            headers = {'Range': f'bytes=-{tail_bytes}'}
            response = self.session.get(url, headers=headers, timeout=15)
            if response.status_code == 206:
                return response.content.decode('utf-8', errors='ignore')
            elif response.status_code == 200:
                content = response.content
                if len(content) > tail_bytes:
                    content = content[-tail_bytes:]
                return content.decode('utf-8', errors='ignore')
        except Exception as e:
            logger.debug(f"[prow_gcs] Could not fetch tail of {url}: {e}")
        return None

    def _artifact_base(self, job_run: JobRun) -> Optional[str]:
        """Build the GCS artifact base URL for a job run.

        Uses gcs_prefix (from SpyglassLink) when available, falling
        back to the standard periodic logs/ path.
        """
        step_name = self._derive_step_name(job_run.job_name)
        if not step_name:
            return None
        if job_run.gcs_prefix:
            return f"{self.gcs_url}/{job_run.gcs_prefix}/artifacts/{step_name}"
        return f"{self.gcs_url}/logs/{job_run.job_name}/{job_run.build_id}/artifacts/{step_name}"

    def _enrich_job_run(self, job_run: JobRun) -> JobRun:
        """Enrich a JobRun with metadata parsed from GCS step logs."""
        step_name = self._derive_step_name(job_run.job_name)
        job_run.step_name = step_name

        if job_run.status == TestStatus.FAILED and not (
            job_run.failure_reason and job_run.failure_category
        ):
            if job_run.gcs_prefix:
                log_url = f"{self.gcs_url}/{job_run.gcs_prefix}/build-log.txt"
            else:
                log_url = (
                    f"{self.gcs_url}/logs/{job_run.job_name}"
                    f"/{job_run.build_id}/build-log.txt"
                )
            tail = self._fetch_gcs_tail(log_url)
            if tail:
                raw = self._parse_failure_reason(tail)
                if raw:
                    job_run.failure_reason = raw
                    job_run.failed_step = self._extract_failed_step(raw)
                    job_run.failure_category = self._classify_failure(raw)

        if not step_name:
            return job_run

        base = self._artifact_base(job_run)
        if not base:
            return job_run

        job_run.ocp_version = self._try_parse_from_steps(
            base,
            ('ipi-install-install', 'ipi-install-install-aws'),
            self._parse_ocp_version,
        ) or job_run.ocp_version

        job_run.csv_version = self._try_parse_from_steps(
            base,
            ('medik8s-operator-subscribe',),
            self._parse_csv_version,
        ) or job_run.csv_version

        job_run.fbc_image = self._try_parse_from_steps(
            base,
            ('medik8s-catalogsource', 'medik8s-disconnected-catalogsource'),
            self._parse_fbc_image,
        ) or job_run.fbc_image

        return job_run

    def _extract_operator(self, job_name: str, raw_test_name: str = '') -> Optional[str]:
        """Extract operator name from Prow job name, with test-name fallback."""
        job_lower = job_name.lower()
        operators = ['far', 'sbr', 'snr', 'nhc', 'nmo', 'mdr']
        for op in operators:
            if f'-e2e-{op}-' in job_lower or job_lower.endswith(f'-e2e-{op}'):
                return op.upper()

        if raw_test_name:
            test_lower = raw_test_name.lower()
            for op in operators:
                if f'[{op}]' in test_lower or f'[{op},' in test_lower:
                    return op.upper()

        return None

    def collect_job_runs(
        self,
        start_date: datetime,
        end_date: datetime,
        job_patterns: Optional[List[str]] = None,
        versions: Optional[List[str]] = None,
        platforms: Optional[List[str]] = None
    ) -> List[JobRun]:
        """
        Collect job runs using per-job Prow job-history endpoint.

        Instead of downloading the massive prowjobs.js (all jobs across
        all teams), queries each configured job name individually via
        the lightweight job-history HTML endpoint.
        """
        import json as json_mod

        job_runs = []
        job_list = job_patterns if job_patterns else self.job_names

        if not job_list:
            logger.warning("[prow_gcs] No job patterns configured")
            return job_runs

        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=timezone.utc)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)

        for job_name in job_list:
            try:
                url = (
                    f"{self.prow_url}/job-history/gs/"
                    f"{self.bucket}/logs/{job_name}"
                )
                logger.info(f"[prow_gcs] Fetching history: {job_name}")
                response = self.session.get(url, timeout=30)
                if response.status_code != 200:
                    logger.warning(
                        f"[prow_gcs] job-history returned "
                        f"{response.status_code} for {job_name}"
                    )
                    continue

                match = re.search(
                    r'var allBuilds\s*=\s*(\[.*?\]);',
                    response.text,
                    re.DOTALL,
                )
                if not match:
                    logger.warning(
                        f"[prow_gcs] No build data in "
                        f"job-history for {job_name}"
                    )
                    continue

                builds = json_mod.loads(match.group(1))
                logger.info(
                    f"[prow_gcs] {job_name}: {len(builds)} builds"
                )

                version, platform = self._extract_version_platform(
                    job_name
                )
                if versions and version not in versions:
                    continue
                if platforms and platform not in platforms:
                    continue

                for build in builds:
                    started_str = build.get('Started', '')
                    if not started_str:
                        continue

                    start_time = datetime.fromisoformat(
                        started_str.replace('Z', '+00:00')
                    )
                    if start_time < start_date or start_time > end_date:
                        continue

                    result = (build.get('Result') or '').strip().upper()
                    if result in ('PENDING', 'TRIGGERED'):
                        continue
                    if not result:
                        result = 'FAILURE'
                    job_status = (
                        TestStatus.PASSED
                        if result == 'SUCCESS'
                        else TestStatus.FAILED
                    )
                    build_id = str(build.get('ID', 'unknown'))
                    duration = int(build.get('Duration', 0)) // 1_000_000_000

                    job_url = (
                        f"{self.prow_url}/view/gs/"
                        f"{self.bucket}/logs/{job_name}/{build_id}"
                    )

                    run = JobRun(
                        job_name=job_name,
                        build_id=build_id,
                        status=job_status,
                        timestamp=start_time,
                        duration_seconds=duration,
                        version=version,
                        platform=platform,
                        total_tests=0,
                        passed_tests=0,
                        failed_tests=0,
                        skipped_tests=0,
                        job_url=job_url,
                    )
                    try:
                        self._enrich_job_run(run)
                    except Exception as e:
                        logger.warning(f"[prow_gcs] Enrichment failed for {job_name}/{build_id}: {e}")
                    job_runs.append(run)

            except Exception as e:
                logger.error(
                    f"[prow_gcs] Error fetching {job_name}: {e}"
                )

        logger.info(
            f"[prow_gcs] Collected {len(job_runs)} job runs "
            f"across {len(job_list)} jobs"
        )
        return job_runs

    def collect_presubmit_job_runs(
        self,
        start_date: datetime,
        end_date: datetime,
        job_patterns: Optional[List[str]] = None,
        versions: Optional[List[str]] = None,
        platforms: Optional[List[str]] = None
    ) -> List[JobRun]:
        """Collect presubmit job runs using the pr-logs/directory/ endpoint."""
        import json as json_mod

        job_runs = []
        job_list = job_patterns or []
        if not job_list:
            return job_runs

        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=timezone.utc)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)

        for job_name in job_list:
            try:
                url = (
                    f"{self.prow_url}/job-history/gs/"
                    f"{self.bucket}/pr-logs/directory/{job_name}"
                )
                logger.info(f"[prow_gcs] Fetching presubmit history: {job_name}")
                response = self.session.get(url, timeout=30)
                if response.status_code != 200:
                    logger.warning(f"[prow_gcs] job-history returned {response.status_code} for {job_name}")
                    continue

                match = re.search(r'var allBuilds\s*=\s*(\[.*?\]);', response.text, re.DOTALL)
                if not match:
                    logger.warning(f"[prow_gcs] No build data in job-history for {job_name}")
                    continue

                builds = json_mod.loads(match.group(1))
                logger.info(f"[prow_gcs] {job_name}: {len(builds)} presubmit builds")

                version, platform = self._extract_version_platform(job_name)
                if versions and version not in versions:
                    continue
                if platforms and platform not in platforms:
                    continue

                for build in builds:
                    started_str = build.get('Started', '')
                    if not started_str:
                        continue

                    start_time = datetime.fromisoformat(started_str.replace('Z', '+00:00'))
                    if start_time < start_date or start_time > end_date:
                        continue

                    result = (build.get('Result') or '').strip().upper()
                    if result in ('PENDING', 'TRIGGERED'):
                        continue
                    if not result:
                        result = 'FAILURE'
                    job_status = TestStatus.PASSED if result == 'SUCCESS' else TestStatus.FAILED
                    build_id = str(build.get('ID', 'unknown'))
                    duration = int(build.get('Duration', 0)) // 1_000_000_000

                    pr_number = None
                    pr_author = None
                    gcs_prefix = None
                    spyglass_link = build.get('SpyglassLink', '')
                    pr_match = PROW_PR_PATH_RE.search(spyglass_link)
                    if pr_match:
                        pr_number = int(pr_match.group('pr_number'))
                        gcs_prefix = pr_match.group(0)

                    refs = build.get('Refs', {})
                    pulls = refs.get('pulls', [])
                    pr_repo = None
                    refs_org = refs.get('org')
                    refs_repo = refs.get('repo')
                    if refs_org and refs_repo:
                        pr_repo = f"{refs_org}/{refs_repo}"
                    if pulls:
                        pr_author = pulls[0].get('author')
                        if not pr_number:
                            pr_number = pulls[0].get('number')

                    job_url = f"{self.prow_url}{spyglass_link}" if spyglass_link else ""

                    run = JobRun(
                        job_name=job_name,
                        build_id=build_id,
                        status=job_status,
                        timestamp=start_time,
                        duration_seconds=duration,
                        version=version,
                        platform=platform,
                        total_tests=0,
                        passed_tests=0,
                        failed_tests=0,
                        skipped_tests=0,
                        job_type="presubmit",
                        pr_number=pr_number,
                        pr_author=pr_author,
                        pr_repo=pr_repo,
                        gcs_prefix=gcs_prefix,
                        job_url=job_url,
                    )
                    try:
                        self._enrich_job_run(run)
                    except Exception as e:
                        logger.warning(f"[prow_gcs] Enrichment failed for presubmit {job_name}/{build_id}: {e}")
                    job_runs.append(run)

            except Exception as e:
                logger.error(f"[prow_gcs] Error fetching presubmit {job_name}: {e}")

        logger.info(f"[prow_gcs] Collected {len(job_runs)} presubmit job runs across {len(job_list)} jobs")
        return job_runs

    def collect_test_results(
        self,
        start_date: datetime,
        end_date: datetime,
        job_patterns: Optional[List[str]] = None,
        test_names: Optional[List[str]] = None,
        versions: Optional[List[str]] = None,
        platforms: Optional[List[str]] = None
    ) -> List[TestResult]:
        """
        Collect test results from GCS artifacts

        1. Get job runs
        2. For each job, fetch JUnit XML from GCS
        3. Parse test results
        4. Fetch logs for failed tests
        """
        # First get job runs
        job_runs = self.collect_job_runs(start_date, end_date, job_patterns, versions, platforms)

        all_results = []

        # Collect test results in parallel
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(
                    self._fetch_test_results_for_job,
                    job_run, test_names
                ): job_run
                for job_run in job_runs
            }

            for future in as_completed(futures):
                job_run = futures[future]
                try:
                    results = future.result()
                    all_results.extend(results)
                    logger.info(f"[prow_gcs] Collected {len(results)} tests from {job_run.job_name}/{job_run.build_id}")
                except Exception as e:
                    logger.error(f"[prow_gcs] Error fetching tests for {job_run.job_name}: {e}")

        logger.info(f"[prow_gcs] Total test results collected: {len(all_results)}")
        return all_results

    def collect_presubmit_test_results(
        self,
        start_date: datetime,
        end_date: datetime,
        job_patterns: Optional[List[str]] = None,
        test_names: Optional[List[str]] = None,
        versions: Optional[List[str]] = None,
        platforms: Optional[List[str]] = None,
        job_runs: Optional[List[JobRun]] = None,
    ) -> List[TestResult]:
        """Collect test results from presubmit job runs."""
        if job_runs is None:
            job_runs = self.collect_presubmit_job_runs(start_date, end_date, job_patterns, versions, platforms)

        all_results = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self._fetch_test_results_for_job, job_run, test_names): job_run
                for job_run in job_runs
            }
            for future in as_completed(futures):
                job_run = futures[future]
                try:
                    results = future.result()
                    all_results.extend(results)
                    logger.info(f"[prow_gcs] Collected {len(results)} presubmit tests from {job_run.job_name}/{job_run.build_id}")
                except Exception as e:
                    logger.error(f"[prow_gcs] Error fetching presubmit tests for {job_run.job_name}: {e}")

        logger.info(f"[prow_gcs] Total presubmit test results collected: {len(all_results)}")
        return all_results

    def _derive_step_name(self, job_name: str) -> Optional[str]:
        """Derive the Prow multi-stage test step name from the job name.

        For medik8s periodic/presubmit jobs, the step name is the suffix
        after the variant segment (e.g. '4.22-konflux-', '4.22-upgrade-').
        Examples:
          periodic-ci-medik8s-system-tests-main-4.22-konflux-e2e-far-weekly-aws
              -> e2e-far-weekly-aws
          periodic-ci-medik8s-system-tests-main-4.22-upgrade-e2e-far-upgrade-aws
              -> e2e-far-upgrade-aws
          periodic-ci-medik8s-system-tests-main-4.22-disconnected-e2e-far-weekly-aws-disconnected
              -> e2e-far-weekly-aws-disconnected
          pull-ci-medik8s-fence-agents-remediation-main-4.22-openshift-e2e
              -> e2e
        """
        match = re.search(r'\d+\.\d+-(?:konflux|openshift|upgrade|disconnected)-(.+)$', job_name)
        if match:
            return match.group(1)
        return None

    def _fetch_test_results_for_job(
        self,
        job_run: JobRun,
        test_names: Optional[List[str]]
    ) -> List[TestResult]:
        """Fetch test results for a single job from GCS artifacts"""
        results = []

        try:
            artifact_base = self._artifact_base(job_run)

            if artifact_base:
                e2e_url = f"{artifact_base}/e2e-test/"
                junit_files = self._find_junit_files(e2e_url, max_depth=3)
                for junit_url in junit_files:
                    tests = self._parse_junit_xml(junit_url, job_run, test_names)
                    results.extend(tests)
                return results

            # Fallback: broad search (for non-medik8s jobs or unknown layout)
            if job_run.gcs_prefix:
                fallback_url = f"{self.gcs_url}/{job_run.gcs_prefix}/artifacts/"
            else:
                fallback_url = f"{self.gcs_url}/logs/{job_run.job_name}/{job_run.build_id}/artifacts/"
            junit_files = self._find_junit_files(fallback_url)
            for junit_url in junit_files:
                tests = self._parse_junit_xml(junit_url, job_run, test_names)
                results.extend(tests)

        except Exception as e:
            logger.error(f"[prow_gcs] Error fetching test results for {job_run.job_name}/{job_run.build_id}: {e}")

        return results

    def _find_junit_files(self, artifacts_url: str, max_depth: int = 5, current_depth: int = 0) -> List[str]:
        """Find JUnit XML files in artifacts directory (recursive search up to 5 levels)"""
        junit_files = []

        if current_depth >= max_depth:
            return junit_files

        try:
            logger.debug(f"[prow_gcs] Searching for junit files at depth {current_depth}: {artifacts_url}")
            response = self.session.get(artifacts_url, timeout=30)
            if response.status_code != 200:
                logger.warning(f"[prow_gcs] Non-200 response ({response.status_code}) from {artifacts_url}")
                return junit_files

            html = response.text

            # Find XML files in current directory (test results may not have "junit" in name)
            xml_pattern = r'href="([^"]*\.xml)"'
            xml_matches = re.findall(xml_pattern, html, re.IGNORECASE)

            for match in xml_matches:
                match = match.strip()

                # Skip ci-operator step metadata (not real test results)
                basename = match.rsplit('/', 1)[-1] if '/' in match else match
                if basename == 'junit_operator.xml':
                    logger.debug(f"[prow_gcs] Skipping ci-operator metadata: {match}")
                    continue
                if basename == 'report_testrun.xml':
                    logger.debug(f"[prow_gcs] Skipping Polarion report (duplicate of Ginkgo JUnit): {match}")
                    continue

                # Build full URL for junit file
                if match.startswith('http'):
                    junit_url = match
                elif match.startswith('/'):
                    # Absolute path - reconstruct from base
                    base_host = artifacts_url.split('/gcs/')[0]
                    junit_url = base_host + match
                else:
                    # Relative path
                    if match.startswith('./'):
                        match = match[2:]
                    junit_url = artifacts_url.rstrip('/') + '/' + match

                logger.debug(f"[prow_gcs] Found junit file: {junit_url}")
                junit_files.append(junit_url)

            # Only recurse if we haven't hit max depth
            if current_depth < max_depth:
                # Find subdirectories (links ending with /)
                dir_pattern = r'href="([^"]+/)"'
                dir_matches = re.findall(dir_pattern, html)

                for match in dir_matches:
                    match = match.strip()

                    # Skip parent directory and non-test directories
                    if match in ['../', '..', '../', 'metadata/']:
                        continue

                    # Build subdirectory URL
                    if match.startswith('http'):
                        subdir_url = match
                    elif match.startswith('/'):
                        # Absolute path - check if it's a child directory
                        base_host = artifacts_url.split('/gcs/')[0]
                        full_path = base_host + match
                        # Only recurse if this is a subdirectory (longer path than current)
                        if not full_path.rstrip('/').startswith(artifacts_url.rstrip('/')):
                            continue
                        if len(full_path.rstrip('/')) <= len(artifacts_url.rstrip('/')):
                            continue
                        subdir_url = full_path
                    else:
                        # Relative path
                        if match.startswith('./'):
                            match = match[2:]
                        subdir_url = artifacts_url.rstrip('/') + '/' + match

                    # Recursively search subdirectory
                    sub_files = self._find_junit_files(subdir_url, max_depth, current_depth + 1)
                    junit_files.extend(sub_files)

        except Exception as e:
            logger.error(f"[prow_gcs] Error finding JUnit files at depth {current_depth} in {artifacts_url}: {e}")

        return junit_files

    def _parse_junit_xml(
        self,
        junit_url: str,
        job_run: JobRun,
        test_names: Optional[List[str]]
    ) -> List[TestResult]:
        """Parse JUnit XML file and extract test results"""
        results = []

        try:
            response = self.session.get(junit_url, timeout=10)
            if response.status_code != 200:
                return results

            root = ET.fromstring(response.content)

            # Parse testsuites or testsuite
            testsuites = root.findall('.//testsuite')
            if not testsuites:
                testsuites = [root] if root.tag == 'testsuite' else []

            for testsuite in testsuites:
                for testcase in testsuite.findall('testcase'):
                    raw_test_name = testcase.get('name', 'unknown')

                    # Only include tests matching test_suite_filter (check raw name before extraction)
                    test_filter = self.config.get('test_suite_filter', '')
                    if test_filter and test_filter not in raw_test_name:
                        continue

                    # Skip empty test names
                    raw_test_name = raw_test_name.strip()
                    if not raw_test_name:
                        continue

                    # Skip Ginkgo infrastructure/lifecycle entries (not real tests)
                    skip_prefixes = ('[BeforeSuite]', '[AfterSuite]',
                                     '[SynchronizedBeforeSuite]', '[SynchronizedAfterSuite]',
                                     '[ReportAfterSuite]', '[ReportBeforeSuite]',
                                     '[BeforeEach]', '[AfterEach]',
                                     '[BeforeAll]', '[AfterAll]',
                                     '[JustBeforeEach]', '[JustAfterEach]',
                                     '[DeferCleanup]', '[CleanupAfterEach]',
                                     '[CleanupAfterAll]')
                    if any(raw_test_name.startswith(p) for p in skip_prefixes):
                        continue

                    # Extract clean test name, description, and Polarion ID
                    test_name, test_description, polarion_id = self._extract_test_name(raw_test_name)

                    # Filter by test name pattern
                    if test_names and test_name not in test_names:
                        continue

                    # Determine status
                    failure = testcase.find('failure')
                    skipped = testcase.find('skipped')
                    error = testcase.find('error')
                    system_out = testcase.find('system-out')

                    if failure is not None:
                        status = TestStatus.FAILED
                        # Include failure message + text + system-out (stdout)
                        error_msg = failure.get('message', '') + '\n' + (failure.text or '')
                        if system_out is not None and system_out.text:
                            error_msg += '\n\nTest Output:\n' + system_out.text
                    elif error is not None:
                        status = TestStatus.ERROR
                        error_msg = error.get('message', '') + '\n' + (error.text or '')
                        if system_out is not None and system_out.text:
                            error_msg += '\n\nTest Output:\n' + system_out.text
                    elif skipped is not None:
                        status = TestStatus.SKIPPED
                        error_msg = None
                    else:
                        status = TestStatus.PASSED
                        error_msg = None

                    # Duration
                    duration_str = testcase.get('time', '0')
                    try:
                        duration = float(duration_str)
                    except ValueError:
                        duration = 0

                    # Construct log_url using same logic as artifacts_url
                    if job_run.job_url and '/view/gs/qe-private-deck/' in job_run.job_url:
                        gcs_path = job_run.job_url.split('/view/gs/qe-private-deck/')[-1]
                        log_url = f"{self.gcs_url}/{gcs_path}/build-log.txt"
                    else:
                        log_url = f"{self.gcs_url}/logs/{job_run.job_name}/{job_run.build_id}/build-log.txt"

                    operator = self._extract_operator(job_run.job_name, raw_test_name)

                    result = TestResult(
                        test_name=test_name,
                        status=status,
                        timestamp=job_run.timestamp,
                        duration_seconds=duration,
                        error_message=error_msg,
                        job_name=job_run.job_name,
                        build_id=job_run.build_id,
                        version=job_run.version,
                        platform=job_run.platform,
                        test_description=test_description,
                        polarion_id=polarion_id,
                        operator=operator,
                        job_type=job_run.job_type,
                        pr_number=job_run.pr_number,
                        job_url=job_run.job_url,
                        log_url=log_url
                    )

                    results.append(result)

        except Exception as e:
            logger.error(f"[prow_gcs] Error parsing JUnit XML {junit_url}: {e}")

        return results

    def _fetch_test_logs(self, job_run: JobRun) -> str:
        """Fetch build logs for a failed test"""
        try:
            log_url = f"{self.gcs_url}/logs/{job_run.job_name}/{job_run.build_id}/build-log.txt"
            response = self.session.get(log_url, timeout=30)

            if response.status_code == 200:
                # Return last 5000 characters (full log can be huge)
                return response.text[-5000:]

        except Exception as e:
            logger.error(f"[prow_gcs] Error fetching logs for {job_run.job_name}/{job_run.build_id}: {e}")

        return "Logs not available"
