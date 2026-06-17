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
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from .base import BaseCollector, TestResult, JobRun, TestStatus

logger = logging.getLogger(__name__)


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

    def _extract_test_name(self, raw_name: str) -> tuple:
        """
        Extract clean test name and description from raw name.

        Handles both OCP-XXXXX format (upstream) and Ginkgo format (medik8s):
        - "OCP-25593:sgao:...[sig-windows] Prevent scheduling..."
        - "[It] [FAR] Verify FenceAgentsRemediation CR remediation flow"

        Returns: (test_id, description)
        """
        ocp_match = re.search(r'OCP-\d+', raw_name)

        if ocp_match:
            test_id = ocp_match.group(0)
            after_id = raw_name.split(test_id, 1)[-1]
            description = after_id.strip(':- \t')
            description = re.sub(r'\s*\[[^\]]+\]', '', description)
            description = re.sub(r'^[:\-\s]+', '', description)
            return test_id, description.strip() if description else test_id

        description = raw_name.strip()
        description = re.sub(r'^\[It\]\s*', '', description)
        description = re.sub(r'\s*\[(Slow|Serial|Disruptive|Flaky|sig-[\w-]+)\]', '', description, flags=re.IGNORECASE)
        description = description.strip()
        test_id = description if description else raw_name
        return test_id, description

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

                    result = build.get('Result', 'FAILURE')
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

                    job_runs.append(JobRun(
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
                    ))

            except Exception as e:
                logger.error(
                    f"[prow_gcs] Error fetching {job_name}: {e}"
                )

        logger.info(
            f"[prow_gcs] Collected {len(job_runs)} job runs "
            f"across {len(job_list)} jobs"
        )
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

    def _fetch_test_results_for_job(
        self,
        job_run: JobRun,
        test_names: Optional[List[str]]
    ) -> List[TestResult]:
        """Fetch test results for a single job from GCS artifacts"""
        results = []

        try:
            # Extract GCS path from job_url to support both /logs/ and /pr-logs/ paths
            if job_run.job_url and '/view/gs/qe-private-deck/' in job_run.job_url:
                # Extract path after /view/gs/qe-private-deck/
                # Example: pr-logs/pull/openshift_release/76816/rehearse-76816-.../2037290229743751168
                # or: logs/periodic-ci-.../build_id
                gcs_path = job_run.job_url.split('/view/gs/qe-private-deck/')[-1]
                artifacts_url = f"{self.gcs_url}/{gcs_path}/artifacts/"
            else:
                # Fallback to default /logs/ path for jobs without URL
                artifacts_url = f"{self.gcs_url}/logs/{job_run.job_name}/{job_run.build_id}/artifacts/"

            junit_files = self._find_junit_files(artifacts_url)

            for junit_url in junit_files:
                tests = self._parse_junit_xml(junit_url, job_run, test_names)
                results.extend(tests)

            # JUnit XML already contains the test failure messages, no need to fetch build-log.txt

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

                    # Extract clean test name (OCP-XXXXX) and description
                    test_name, test_description = self._extract_test_name(raw_test_name)

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
