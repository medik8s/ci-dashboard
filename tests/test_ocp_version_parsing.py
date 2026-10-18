"""OCP version resolution regression tests for the Prow GCS collector.

Disconnected install logs lead with a DEFAULT release-image reference that
ci-operator overrides on the next line, so the log-based fallback records
the stale default (4.22) instead of the installed version. The version must
therefore come from the cluster's ClusterVersion artifact. Fixtures are
excerpts of the real artifacts from 5.0-disconnected FAR build
2108347260289945600 and 4.22-disconnected FAR build 2110702622112485376.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from collectors.base import JobRun, TestStatus  # noqa: E402
from collectors.prow_gcs import ProwGCSCollector  # noqa: E402

GCS_URL = 'https://gcsweb.example/gcs/bucket'

# Real excerpt (lines 1-7) of ipi-install-install-aws/build-log.txt from the
# 5.0-disconnected build: the release image on line 6 is a step DEFAULT that
# the override warning on line 7 replaces; the cluster actually ran 5.0.
DISCONNECTED_INSTALL_LOG = (
    '/usr/bin/openshift-install\n'
    'OPENSHIFT_INSTALL_RELEASE_IMAGE_OVERRIDE: registry.build12.ci.openshift.org/ci-op-8n508irv/release@sha256:89b826c66b0f838233cadf08d7039b52ccdad30f4647285637fac9568e7b3dec\n'
    'Installing from release registry.build12.ci.openshift.org/ci-op-8n508irv/release@sha256:89b826c66b0f838233cadf08d7039b52ccdad30f4647285637fac9568e7b3dec\n'
    'openshift-install 5.0-rc-1-21-g1da83dd26f4cb1c04b32aa833e9f648ebbe8eaaa-dirty\n'
    'built from commit 1da83dd26f4cb1c04b32aa833e9f648ebbe8eaaa\n'
    'release image registry.ci.openshift.org/origin/release:4.22\n'
    'level=warning msg=Found override for release image (registry.build12.ci.openshift.org/ci-op-8n508irv/release@sha256:89b826c66b0f838233cadf08d7039b52ccdad30f4647285637fac9568e7b3dec). Release Image Architecture is unknown\n'
)

# Connected workflows print the authoritative line instead.
CONNECTED_INSTALL_LOG = 'Release payload version: 4.22.9\n'

CLUSTERVERSION_5_0 = json.dumps({
    'apiVersion': 'config.openshift.io/v1',
    'kind': 'List',
    'items': [{
        'apiVersion': 'config.openshift.io/v1',
        'kind': 'ClusterVersion',
        'metadata': {'name': 'version'},
        'status': {
            'desired': {'version': '5.0.0-0.nightly-2026-10-06-215158'},
            'history': [
                {'version': '5.0.0-0.nightly-2026-10-06-215158', 'state': 'Completed'},
            ],
        },
    }],
})

CLUSTERVERSION_4_22 = json.dumps({
    'apiVersion': 'config.openshift.io/v1',
    'kind': 'List',
    'items': [{
        'apiVersion': 'config.openshift.io/v1',
        'kind': 'ClusterVersion',
        'metadata': {'name': 'version'},
        'status': {
            'desired': {'version': '4.22.18'},
            'history': [
                {'version': '4.22.18', 'state': 'Completed'},
            ],
        },
    }],
})

# An upgrade cluster reports the post-upgrade target newest-first, after the
# initial N-1 entry.
CLUSTERVERSION_UPGRADED = json.dumps({
    'apiVersion': 'config.openshift.io/v1',
    'kind': 'List',
    'items': [{
        'apiVersion': 'config.openshift.io/v1',
        'kind': 'ClusterVersion',
        'metadata': {'name': 'version'},
        'status': {
            'desired': {'version': '5.0.0-0.nightly-2026-10-13-215253'},
            'history': [
                {'version': '5.0.0-0.nightly-2026-10-13-215253', 'state': 'Completed'},
                {'version': '4.22.18', 'state': 'Completed'},
            ],
        },
    }],
})

STEP_DIRS = {'e2e-test', 'ipi-install-install-aws', 'medik8s-operator-subscribe',
             'medik8s-disconnected-catalogsource', 'gather-must-gather', 'gather-extra'}


def make_collector(url_map):
    collector = ProwGCSCollector.__new__(ProwGCSCollector)
    collector.gcs_url = GCS_URL
    collector._fetch_gcs_text = lambda url: url_map.get(url)
    collector._list_step_subdirs = lambda base: set(STEP_DIRS)
    return collector


def make_run(job_name, version, gcs_prefix):
    return JobRun(
        job_name=job_name,
        build_id='2108347260289945600',
        status=TestStatus.PASSED,
        timestamp=datetime(2026, 10, 9, tzinfo=timezone.utc),
        duration_seconds=3600.0,
        version=version,
        platform='aws',
        total_tests=30,
        passed_tests=30,
        failed_tests=0,
        skipped_tests=0,
        gcs_prefix=gcs_prefix,
    )


class OcpVersionParsingTests(unittest.TestCase):
    def test_disconnected_install_log_alone_yields_stale_default(self):
        """Documents why the install log cannot be trusted for these jobs."""
        collector = ProwGCSCollector.__new__(ProwGCSCollector)
        self.assertEqual(collector._parse_ocp_version(DISCONNECTED_INSTALL_LOG), '4.22')

    def test_clusterversion_reader_returns_full_installed_version(self):
        collector = make_collector({})
        base = f"{GCS_URL}/logs/x/artifacts/e2e-far-weekly-aws-disconnected"
        collector._fetch_gcs_text = lambda url: {
            f'{base}/gather-extra/artifacts/clusterversion.json': CLUSTERVERSION_5_0,
        }.get(url)
        self.assertEqual(collector._parse_clusterversion_version(base, '5.0'),
                         '5.0.0-0.nightly-2026-10-06-215158')

        collector._fetch_gcs_text = lambda url: {
            f'{base}/gather-extra/artifacts/clusterversion.json': CLUSTERVERSION_4_22,
        }.get(url)
        self.assertEqual(collector._parse_clusterversion_version(base, '4.22'), '4.22.18')

    def test_clusterversion_reader_excludes_initial_release_for_upgrades(self):
        collector = make_collector({})
        base = f"{GCS_URL}/logs/x/artifacts/e2e-far-upgrade-aws"
        collector._fetch_gcs_text = lambda url: {
            f'{base}/gather-extra/artifacts/clusterversion.json': CLUSTERVERSION_UPGRADED,
        }.get(url)
        self.assertEqual(collector._parse_clusterversion_version(base, '5.0'),
                         '5.0.0-0.nightly-2026-10-13-215253')

    def test_clusterversion_reader_returns_none_when_artifact_missing(self):
        collector = make_collector({})
        base = f"{GCS_URL}/logs/x/artifacts/e2e-far-weekly-aws-disconnected"
        self.assertIsNone(collector._parse_clusterversion_version(base, '5.0'))

    def test_disconnected_run_prefers_clusterversion_over_install_log(self):
        job_name = ('periodic-ci-openshift-rhwa-system-tests-main-5.0-disconnected-'
                    'e2e-far-weekly-aws-disconnected')
        prefix = 'logs/disconnected-far/2108347260289945600'
        base = f"{GCS_URL}/{prefix}/artifacts/e2e-far-weekly-aws-disconnected"
        collector = make_collector({
            f'{base}/gather-extra/artifacts/clusterversion.json': CLUSTERVERSION_5_0,
            # The install log still carries the stale 4.22 default; it must lose.
            f'{base}/ipi-install-install-aws/build-log.txt': DISCONNECTED_INSTALL_LOG,
        })
        run = make_run(job_name, '5.0', prefix)
        collector._enrich_job_run(run)
        self.assertEqual(run.ocp_version, '5.0.0-0.nightly-2026-10-06-215158')
        self.assertNotEqual(run.ocp_version, '4.22')

    def test_run_without_clusterversion_falls_back_to_install_log(self):
        job_name = ('periodic-ci-openshift-rhwa-system-tests-main-4.22-konflux-'
                    'e2e-far-weekly-aws')
        prefix = 'logs/konflux-far/2108347260289945600'
        base = f"{GCS_URL}/{prefix}/artifacts/e2e-far-weekly-aws"
        collector = make_collector({
            f'{base}/ipi-install-install/build-log.txt': CONNECTED_INSTALL_LOG,
        })
        run = make_run(job_name, '4.22', prefix)
        collector._enrich_job_run(run)
        self.assertEqual(run.ocp_version, '4.22.9')


if __name__ == '__main__':
    unittest.main()
